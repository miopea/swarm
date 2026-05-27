"""Helpers used only by ``_handle_view_message_stream``.

Extracted from ``_messages.py`` (task #519) to keep that module under
the per-module LOC budget. The render + structured-payload helpers
were already split for cyclomatic-complexity reasons (task #237 added
the ``full`` branch); moving them to a sibling module is the natural
next step now that the surface is per-concern.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


_IDLE_RECIPIENT_STATES = ("RESTING", "SLEEPING", "STUNG")


def _message_stream_worker_states(d: SwarmDaemon) -> dict[str, str]:
    """Map worker-name → display_state string for the in-memory workers."""
    out: dict[str, str] = {}
    for w in getattr(d, "workers", []) or []:
        state = getattr(w, "display_state", None) or getattr(w, "state", None)
        if state is not None and hasattr(state, "value"):
            out[w.name] = state.value
        elif state is not None:
            out[w.name] = str(state)
    return out


def _render_message_stream_rows(
    rows: list[Any],
    *,
    worker_state: dict[str, str],
    actionable_only: bool,
    limit: int,
    full: bool,
) -> list[str]:
    """Format message-stream rows into display lines.

    Extracted from ``_handle_view_message_stream`` to keep the handler's
    complexity under the lint cap (task #237 added the ``full`` branch
    and pushed it over).
    """
    lines: list[str] = []
    for r in rows:
        recipient = r["recipient"]
        recipient_state = worker_state.get(recipient, "UNKNOWN")
        has_read = r["read_at"] is not None
        if actionable_only:
            if has_read or recipient_state not in _IDLE_RECIPIENT_STATES:
                continue
            if len(lines) >= limit:
                break
        flag = "READ" if has_read else "UNREAD"
        content = r["content"] or ""
        body = content if full else content[:160]
        header = f"[{r['msg_type']}] {r['sender']} → {recipient} ({recipient_state}, {flag})"
        lines.append(f"{header}:\n{body}" if full else f"{header}: {body}")
    return lines


def _structured_message_stream_rows(
    rows: list[Any],
    *,
    worker_state: dict[str, str],
    actionable_only: bool,
    limit: int,
) -> list[dict[str, Any]]:
    """Companion to ``_render_message_stream_rows`` returning structured payload.

    Same filter logic; emits one dict per visible row with the full
    message body (truncation is text-only) and the recipient's state
    joined in. Kept separate from the rendering helper so the text and
    structured shapes can evolve independently if needed.
    """
    out: list[dict[str, Any]] = []
    for r in rows:
        recipient = r["recipient"]
        recipient_state = worker_state.get(recipient, "UNKNOWN")
        has_read = r["read_at"] is not None
        if actionable_only:
            if has_read or recipient_state not in _IDLE_RECIPIENT_STATES:
                continue
            if len(out) >= limit:
                break
        out.append(
            {
                "id": r["id"],
                "msg_type": r["msg_type"],
                "sender": r["sender"],
                "recipient": recipient,
                "recipient_state": recipient_state,
                "read": has_read,
                "content": r["content"] or "",
                "created_at": r["created_at"],
                "read_at": r["read_at"],
            }
        )
    return out
