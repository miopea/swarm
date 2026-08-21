"""Canonical resolution of the Swarm (legacy) state directory.

Every runtime path — the SQLite database, the pty-holder socket, logs,
uploads, the Queen workdir — hangs off one directory.  Before the
relocation that directory was hardcoded as ``~/.swarm`` in ~50 places,
which is why moving it required a command rather than a config edit.

Resolution order, most explicit first:

1. ``$SWARM_STATE_DIR`` — an explicit override.  Used by tests and by
   operators running more than one hive on a box.
2. ``~/.swarm-legacy`` **if it exists** — the post-relocation home.  Its
   existence is the marker; there is no separate flag file to fall out of
   sync with reality.
3. ``~/.swarm`` — the pre-relocation home, still correct for every
   install that has not run ``swarm relocate``.

Rule 2 is checked before rule 3 on purpose.  After a relocation something
else may well create a fresh ``~/.swarm`` (that is the point of freeing
the name); preferring the relocated directory means Legacy keeps reading
its own state instead of silently adopting an empty stranger.

Callers should treat the result as stable for the life of the process.
``swarm relocate`` moves the directory and then re-execs, so nothing has
to cope with the answer changing underneath it.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_VAR = "SWARM_STATE_DIR"
RELOCATED_NAME = ".swarm-legacy"
ORIGINAL_NAME = ".swarm"


def original_state_dir() -> Path:
    """The pre-relocation directory, whether or not it still exists."""
    return Path.home() / ORIGINAL_NAME


def relocated_state_dir() -> Path:
    """The post-relocation directory, whether or not it exists yet."""
    return Path.home() / RELOCATED_NAME


def state_dir() -> Path:
    """Return the directory holding all Swarm (legacy) runtime state."""
    override = os.environ.get(ENV_VAR)
    if override:
        return Path(override).expanduser()
    relocated = relocated_state_dir()
    if relocated.is_dir():
        return relocated
    return original_state_dir()


def is_relocated() -> bool:
    """True when this hive lives in the relocated directory.

    Keyed on the resolved directory's *name*, not on how it was chosen, so
    an explicit ``$SWARM_STATE_DIR`` pointing somewhere custom counts as
    neither relocated nor original — which is right, because the unit and
    entrypoint names only follow the ``.swarm-legacy`` convention.
    """
    return state_dir().name == RELOCATED_NAME


def state_path_str(*parts: str) -> str:
    """A config-friendly string for a path inside the state directory.

    Returned ``~``-anchored whenever the state directory sits directly
    under the home directory, because these strings get **serialized into
    the user's config**.  Writing an absolute path there would freeze
    today's location into the file, so a later relocation would leave the
    config pointing at a directory that no longer exists.  Keeping the
    ``~/<dir>/...`` shape means the value stays correct as long as the
    directory name is right.
    """
    home = Path.home()
    base = state_dir()
    try:
        rel = base.relative_to(home)
    except ValueError:
        return str(base.joinpath(*parts))
    return "/".join(["~", str(rel), *parts])
