"""Anonymous install ID — one UUID per swarm installation, persisted locally.

Lets the maintainer correlate multiple reports from the same install
without collecting any personally identifying information.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from swarm.paths import state_dir

_INSTALL_ID_PATH = state_dir() / "install-id"


def get_install_id(path: Path | None = None) -> str:
    """Return the stable install ID, creating it on first call.

    The ID is a UUID4 stored at ``~/.swarm/install-id``. If the file
    can't be read or written (e.g., permissions), returns a fresh
    in-memory UUID so the caller always has something to show.
    """
    target = path or _INSTALL_ID_PATH
    try:
        if target.exists():
            content = target.read_text(encoding="utf-8").strip()
            if content:
                return content
    except OSError:
        pass

    new_id = str(uuid.uuid4())
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_id + "\n", encoding="utf-8")
    except OSError:
        # Non-fatal: return the ID even if we couldn't persist it
        pass
    return new_id
