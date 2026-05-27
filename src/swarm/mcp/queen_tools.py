"""Queen-only MCP tool registry + dispatcher.

Per-domain schemas + handlers live in
:mod:`swarm.mcp.queen_handlers`. This module aggregates them into the
unified ``QUEEN_TOOLS`` and ``QUEEN_HANDLERS`` symbols that
:mod:`swarm.mcp.tools` folds into the published MCP registry.

Task #519 split the monolithic ``queen_tools.py`` into per-concern
handler modules. Tests reach for ``_assert_queen``, ``_clamp``, and
``_handle_view_worker_state`` by name from this module — those are
re-exported below so the split is invisible to existing call sites.
"""

from __future__ import annotations

from typing import Any

from swarm.mcp.queen_handlers._common import (  # noqa: F401
    _PERMISSION_DENIED,
    _assert_queen,
    _clamp,
)
from swarm.mcp.queen_handlers._learnings import HANDLERS as _LEARN_H
from swarm.mcp.queen_handlers._learnings import TOOLS as _LEARN_T
from swarm.mcp.queen_handlers._logs import HANDLERS as _LOGS_H
from swarm.mcp.queen_handlers._logs import TOOLS as _LOGS_T
from swarm.mcp.queen_handlers._messages import HANDLERS as _MSG_H
from swarm.mcp.queen_handlers._messages import TOOLS as _MSG_T
from swarm.mcp.queen_handlers._tasks import HANDLERS as _TASKS_H
from swarm.mcp.queen_handlers._tasks import TOOLS as _TASKS_T
from swarm.mcp.queen_handlers._threads import HANDLERS as _THREADS_H
from swarm.mcp.queen_handlers._threads import TOOLS as _THREADS_T
from swarm.mcp.queen_handlers._views import HANDLERS as _VIEWS_H
from swarm.mcp.queen_handlers._views import TOOLS as _VIEWS_T
from swarm.mcp.queen_handlers._views import _handle_view_worker_state  # noqa: F401
from swarm.mcp.queen_handlers._workers import HANDLERS as _WORKERS_H
from swarm.mcp.queen_handlers._workers import TOOLS as _WORKERS_T

QUEEN_TOOLS: list[dict[str, Any]] = [
    *_VIEWS_T,
    *_LOGS_T,
    *_MSG_T,
    *_THREADS_T,
    *_LEARN_T,
    *_WORKERS_T,
    *_TASKS_T,
]


QUEEN_HANDLERS: dict[str, Any] = {
    **_VIEWS_H,
    **_LOGS_H,
    **_MSG_H,
    **_THREADS_H,
    **_LEARN_H,
    **_WORKERS_H,
    **_TASKS_H,
}
