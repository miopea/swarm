"""Per-domain Queen MCP handlers (task #519 split from ``mcp/queen_tools.py``).

Each `_*.py` module owns the MCP tool *schemas* AND the handler functions
for a single Queen concern. The aggregator at :mod:`swarm.mcp.queen_tools`
imports their ``TOOLS`` lists and ``HANDLERS`` dicts and merges them into
the unified ``QUEEN_TOOLS`` / ``QUEEN_HANDLERS`` symbols that
:mod:`swarm.mcp.tools` folds into the published MCP registry.

Mirrors the layout of :mod:`swarm.mcp.handlers` introduced in task #518.
"""
