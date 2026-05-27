"""Helpers shared by the thread handlers + the learnings save handler.

Extracted from ``_threads.py`` (task #519) to keep ``_threads.py`` under
the per-module LOC budget. ``_learnings.py`` imports
``_resolve_thread_alias`` from here too — the Queen often saves a
learning against the operator-alias thread.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


_DEFAULT_OPERATOR_THREAD_ALIAS = "operator"
_DEFAULT_OPERATOR_THREAD_TITLE = "Operator chat"


def _ensure_operator_thread(d: SwarmDaemon) -> str:
    """Return the id of the default operator thread, creating it if needed.

    The Queen references this thread via the alias ``"operator"`` so she
    doesn't need to remember a uuid between sessions.  The alias maps to
    the single most-recent active ``kind='operator'`` thread; if none
    exists we create one.
    """
    store = d.queen_chat
    active = store.list_threads(kind="operator", status="active", limit=1)
    if active:
        return active[0].id
    thread = store.create_thread(
        title=_DEFAULT_OPERATOR_THREAD_TITLE,
        kind="operator",
    )
    _broadcast_thread_event(d, thread.id, "created")
    return thread.id


def _broadcast_thread_event(d: SwarmDaemon, thread_id: str, event: str) -> None:
    """Push a ``queen.thread`` WS event for the chat panel to react to.

    ``event`` is one of ``created|updated|resolved``. Safe to swallow
    failures — the UI polls on reconnect.
    """
    store = getattr(d, "queen_chat", None)
    if store is None:
        return
    try:
        thread = store.get_thread(thread_id)
        if thread is None:
            return
        d.broadcast_ws({"type": "queen.thread", "event": event, "thread": thread.to_dict()})
    except Exception:
        pass


def _broadcast_message_event(d: SwarmDaemon, thread_id: str, message_dict: dict[str, Any]) -> None:
    """Push a ``queen.message`` WS event for a newly-added message."""
    try:
        d.broadcast_ws({"type": "queen.message", "thread_id": thread_id, "message": message_dict})
    except Exception:
        pass


def _resolve_thread_alias(d: SwarmDaemon, raw: str) -> str | None:
    """Translate a thread_id or alias to a real thread id.

    Returns ``None`` when the target can't be resolved.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw == _DEFAULT_OPERATOR_THREAD_ALIAS:
        return _ensure_operator_thread(d)
    # Real id — just confirm it exists.
    if d.queen_chat.get_thread(raw) is None:
        return None
    return raw
