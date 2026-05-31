"""Queen session persistence — save/restore session IDs.

Uses swarm.db (queen_sessions table) with file-based fallback.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from swarm.logging import get_logger

_log = get_logger("queen.session")

STATE_DIR = Path.home() / ".swarm" / "queen"


def save_session(session_name: str, session_id: str) -> None:
    if _save_to_db(session_name, session_id):
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{session_name}.json"
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps({"session_id": session_id}))
    os.replace(tmp, path)


def load_session(session_name: str) -> str | None:
    result = _load_from_db(session_name)
    if result is not None:
        return result
    path = STATE_DIR / f"{session_name}.json"
    if path.exists():
        try:
            data = json.loads(path.read_text())
            return data.get("session_id")
        except (json.JSONDecodeError, OSError, KeyError):
            return None
    return None


def clear_session(session_name: str) -> None:
    _clear_from_db(session_name)
    path = STATE_DIR / f"{session_name}.json"
    if path.exists():
        path.unlink()


def _save_to_db(name: str, session_id: str) -> bool:
    try:
        from swarm.db.core import _DEFAULT_DB_PATH, SwarmDB

        if not _DEFAULT_DB_PATH.exists():
            return False
        db = SwarmDB()
        db.execute(
            "INSERT OR REPLACE INTO queen_sessions (name, session_id, created_at) VALUES (?, ?, ?)",
            (name, session_id, time.time()),
        )
        db.commit()
        db.close()
        return True
    except Exception:
        _log.warning("failed to save queen session %r to DB", name, exc_info=True)
        return False


def _load_from_db(name: str) -> str | None:
    try:
        from swarm.db.core import _DEFAULT_DB_PATH, SwarmDB

        if not _DEFAULT_DB_PATH.exists():
            return None
        db = SwarmDB()
        row = db.fetchone(
            "SELECT session_id FROM queen_sessions WHERE name = ?",
            (name,),
        )
        db.close()
        return row[0] if row else None
    except Exception:
        _log.warning("failed to load queen session %r from DB", name, exc_info=True)
        return None


def _clear_from_db(name: str) -> None:
    try:
        from swarm.db.core import _DEFAULT_DB_PATH, SwarmDB

        if not _DEFAULT_DB_PATH.exists():
            return
        db = SwarmDB()
        db.delete("queen_sessions", "name = ?", (name,))
        db.close()
    except Exception:
        _log.warning("failed to clear queen session %r from DB", name, exc_info=True)
