"""TypedDict definitions for MCP handler return shapes.

Defined in one shared module so the worker + Queen handler families
both reach for the same content types — every ``_handle_*`` function
returns ``list[TextContent]`` (legacy bare shape) or
``StructuredResponse`` (the Phase-3 wrapper that carries a
structuredContent sidecar Claude Code 2.1.x prefers).

TypedDicts are erased at runtime, so adding these annotations does
NOT change a single byte of the JSON the MCP server emits — verified
by diffing tool output before/after the task #520 sweep.
"""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict


class TextContent(TypedDict):
    """A single text-typed MCP content block (the only content kind the
    swarm surface emits today). The MCP protocol also defines ``image``
    and ``resource_link`` blocks; if the swarm starts emitting those,
    promote this to a Union of per-kind TypedDicts."""

    type: Literal["text"]
    text: str


# Errors are returned as TextContent with a human-readable text body
# starting with "Error:", "Missing 'foo'", etc. — no schema distinction
# at the MCP layer. Aliased for documentation clarity.
ErrorContent = TextContent


class StructuredResponse(TypedDict):
    """Wrapped MCP response — bare text blocks plus an optional JSON
    sidecar Claude Code 2.1.x clients query directly without re-parsing
    the text. The wrapper is what Phase 3 (2026-05-08) introduced."""

    content: list[TextContent]
    structuredContent: NotRequired[dict[str, object]]
    _meta: NotRequired[dict[str, object]]


# The return-type alias every handler signs. Use this on every
# ``def _handle_*(...) -> HandlerResult:`` signature whose body may
# return either shape.
HandlerResult = list[TextContent] | StructuredResponse
