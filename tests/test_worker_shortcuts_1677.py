"""#1677 — operator-defined PTY shortcuts, settable in config.

The operator asked for Shift+Tab as a shortcut and clarified: settable in CONFIG, as a
global option in the shortcut bar. Shift+Tab is `\\x1b[Z` (CSI Z, back-tab) — Claude Code's
PERMISSION-MODE CYCLE, which is why this is not cosmetic: #1647 measured 18 of 18 running
workers in auto mode, where the drone escalate-guards abstain to a classifier that does not
implement them, and surfaced `permission_mode` so the dashboard can SHOW the mode. This is
the other half.

TWO DESIGN CONSTRAINTS, both learned the hard way and both tested here:

  KEYS COME FROM CONFIG, NEVER FROM THE REQUEST. Accepting `keys` from the caller would
  make this a general write-arbitrary-bytes-to-any-PTY endpoint reachable by anything
  holding a session cookie — far more surface than the feature needs.

  THE SHORTCUT REFUSES ON AN OPEN PICKER. The ordinary operator write path deliberately
  BYPASSES the #1451 hold, because the operator IS the human the prompt waits for and a
  guard there would make an open question unanswerable. But a shortcut is a button press,
  not a considered answer to the question on screen — firing one into a picker is how the
  operator's own question gets answered by accident (#1443's shape). It REFUSES rather
  than queues, for the reason #1608 and #1623 settled: a discrete action delivered
  whenever the prompt happens to close arrives with no relation to why it was sent.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.config.models import HiveConfig, WorkerShortcut
from swarm.config.serialization import serialize_config
from swarm.pty.process import WorkerProcess

SHIFT_TAB = "\x1b[Z"

REAL_PICKER = """\
  Would you like to proceed?
❯ 1. Yes, and use auto mode
   2. Yes, manually approve edits
   3. Tell Claude what to change
"""


# ---------------------------------------------------------------------------
# AC1 — defined in config, no code change to add one
# ---------------------------------------------------------------------------


def test_a_shortcut_round_trips_through_config_serialization():
    """`serialize_config` is explicit per field, so a new list only survives if it was
    wired. If this fails, a shortcut added by an operator silently vanishes on save."""
    cfg = HiveConfig(shortcuts=[WorkerShortcut(label="Cycle permission mode", keys=SHIFT_TAB)])

    data = serialize_config(cfg)

    assert data["shortcuts"] == [{"label": "Cycle permission mode", "keys": SHIFT_TAB}]


def test_the_escape_sequence_survives_serialization_intact():
    """The whole payload is a control sequence. A serializer that escaped, stripped or
    normalised it would leave the shortcut firing the wrong bytes — and `\\x1b[Z` mangled
    into literal text would type `[Z` into the worker's prompt."""
    cfg = HiveConfig(shortcuts=[WorkerShortcut(label="st", keys=SHIFT_TAB)])

    assert serialize_config(cfg)["shortcuts"][0]["keys"] == "\x1b[Z"
    assert len(serialize_config(cfg)["shortcuts"][0]["keys"]) == 3


def _write_yaml(tmp_path, body: str):
    """A real swarm.yaml on disk, loaded through the real loader.

    Deliberately not a dict helper: the operator edits a FILE, and the question AC1 asks
    is whether a shortcut added that way takes effect. Parsing a dict would skip the YAML
    layer where an escape sequence is most likely to be mangled.
    """
    path = tmp_path / "swarm.yaml"
    path.write_text(body)
    return path


def test_shortcuts_load_from_a_real_yaml_file(tmp_path):
    """AC1's core: an operator adds a shortcut in config and it becomes usable, with no
    code change."""
    from swarm.config.loader import load_config

    cfg = load_config(
        str(
            _write_yaml(
                tmp_path,
                "session_name: t\nworkers: []\n"
                "shortcuts:\n"
                '  - label: "Cycle permission mode"\n'
                '    keys: "\\e[Z"\n',
            )
        )
    )

    assert [s.label for s in cfg.shortcuts] == ["Cycle permission mode"]
    assert cfg.shortcuts[0].keys in ("\x1b[Z", "\\e[Z")


def test_an_incomplete_shortcut_entry_is_dropped_rather_than_half_built(tmp_path):
    """A shortcut with a label and no keys would render a button that writes nothing —
    worse than absent, because it looks like it works."""
    from swarm.config.loader import load_config

    cfg = load_config(
        str(
            _write_yaml(
                tmp_path,
                "session_name: t\nworkers: []\n"
                "shortcuts:\n"
                '  - label: "broken"\n'
                '  - keys: "x"\n'
                '  - label: "ok"\n    keys: "y"\n',
            )
        )
    )

    assert [s.label for s in cfg.shortcuts] == ["ok"]


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


def _daemon(screen: str = "just working\n", shortcuts=None):
    d = MagicMock()
    d.config.shortcuts = (
        shortcuts
        if shortcuts is not None
        else [WorkerShortcut(label="Cycle permission mode", keys=SHIFT_TAB)]
    )
    worker = MagicMock()
    worker.name = "swarm"
    # SPEC'D against the real class on purpose. A bare MagicMock accepts any kwarg the
    # real object rejects — which is exactly how the first version of this endpoint
    # passed every unit test and then raised
    # `send_keys() got an unexpected keyword argument 'actor'` against the live daemon.
    # The mock has to refuse what WorkerProcess would refuse.
    worker.process = MagicMock(spec=WorkerProcess)
    worker.process.get_content.return_value = screen
    worker.process.send_keys = AsyncMock(spec=WorkerProcess.send_keys, return_value=True)
    d.workers = [worker]
    return d, worker


async def _fire(daemon, label: str = "Cycle permission mode", name: str = "swarm"):
    from swarm.server.routes.workers import handle_worker_shortcut

    request = MagicMock()
    request.match_info = {"name": name}
    request.json = AsyncMock(return_value={"label": label})
    request.app = {"daemon": daemon}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("swarm.server.helpers.get_daemon", lambda _r: daemon)
        mp.setattr("swarm.server.routes.workers.get_daemon", lambda _r: daemon)
        return await handle_worker_shortcut(request)


@pytest.mark.asyncio
async def test_firing_a_shortcut_writes_the_configured_bytes_with_an_actor():
    """AC3. The write must be attributable in ~/.swarm/pty-writes.jsonl — a bare control
    sequence arriving with no name is exactly what #1658 exists to prevent."""
    daemon, worker = _daemon()

    resp = await _fire(daemon)

    assert resp.status == 200
    worker.process.send_keys.assert_awaited_once()
    kwargs = worker.process.send_keys.await_args.kwargs
    assert worker.process.send_keys.await_args.args[0] == SHIFT_TAB
    assert kwargs["actor"] == "operator-shortcut"
    assert kwargs["enter"] is False, "a shortcut must not append Enter — that would submit"


@pytest.mark.asyncio
async def test_an_open_picker_refuses_the_shortcut_and_writes_NOTHING():
    """AC4, and the load-bearing half. Asserted on the WRITE PATH, not just the status
    code: a handler that returned 409 and still wrote would pass a response-only test
    while committing the operator's picker to an answer."""
    daemon, worker = _daemon(screen=REAL_PICKER)

    resp = await _fire(daemon)

    assert resp.status == 409
    worker.process.send_keys.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_refusal_names_what_to_do_about_it():
    """A refusal that only says no leaves the operator stuck — the #1608 lesson."""
    daemon, _ = _daemon(screen=REAL_PICKER)

    body = json.loads((await _fire(daemon)).body)

    assert "open selection prompt" in body["error"]
    assert "dismiss" in body["error"].lower() or "answer it" in body["error"].lower()


@pytest.mark.asyncio
async def test_an_unknown_label_is_refused_and_lists_what_is_configured():
    daemon, worker = _daemon()

    resp = await _fire(daemon, label="not-a-shortcut")

    assert resp.status == 404
    assert "Cycle permission mode" in json.loads(resp.body)["error"]
    worker.process.send_keys.assert_not_awaited()


@pytest.mark.asyncio
async def test_keys_supplied_by_the_caller_are_ignored():
    """THE SECURITY PROPERTY. The endpoint resolves bytes from config by label; if it
    honoured a caller-supplied `keys` it would be an arbitrary-PTY-write API for anyone
    with a session cookie."""
    from swarm.server.routes.workers import handle_worker_shortcut

    daemon, worker = _daemon()
    request = MagicMock()
    request.match_info = {"name": "swarm"}
    request.json = AsyncMock(return_value={"label": "Cycle permission mode", "keys": "rm -rf /\r"})
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("swarm.server.routes.workers.get_daemon", lambda _r: daemon)
        await handle_worker_shortcut(request)

    assert worker.process.send_keys.await_args.args[0] == SHIFT_TAB


@pytest.mark.asyncio
async def test_no_configured_shortcuts_refuses_rather_than_guessing():
    daemon, worker = _daemon(shortcuts=[])

    resp = await _fire(daemon)

    assert resp.status == 404
    worker.process.send_keys.assert_not_awaited()
