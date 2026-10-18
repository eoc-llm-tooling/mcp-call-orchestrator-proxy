"""Input schemas of the tools the proxy last listed to its clients.

Before dispatching a 2026-07-28 ``tools/call`` that carries arguments or
``Mcp-Param-*`` headers, the MCP SDK's streamable-HTTP server checks those
headers against the called tool's input schema. A server without a
``get_tool_input_schema`` lookup has the SDK run its own ``tools/list`` handler
to find the schema; on this proxy that handler lists the backend through the
queue, so each such call would put a listing on the backend's queue ahead of
itself. ``ListedToolSchemas`` records the tool list the proxy last served, after
exposure control, and answers the lookup from it, never from the backend.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool
from mcp import types as mcp_types


class ListedToolSchemas(Middleware):
    """Records each tool listing the proxy serves and looks schemas up in it."""

    def __init__(self) -> None:
        self._schemas: dict[str, dict[str, Any]] = {}

    async def on_list_tools(
        self,
        context: MiddlewareContext[mcp_types.ListToolsRequest],
        call_next: CallNext[mcp_types.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        self._schemas = {tool.name: tool.parameters for tool in tools}
        return tools

    def input_schema(self, name: str) -> Mapping[str, Any] | None:
        """The input schema of ``name`` as the last listing served it.

        ``None`` for a tool that listing did not carry, a hidden one included;
        the SDK then skips the header check and dispatch answers for the tool. A
        conforming client sends ``Mcp-Param-*`` headers only for a tool it has
        listed itself, and its listing passes through here.
        """
        return self._schemas.get(name)
