"""A real MCP server that implements tools and nothing else, served over stdio.

Built on the MCP SDK's low-level ``Server`` rather than FastMCP: a FastMCP server
always answers prompt and resource listings (with empty lists), while a server
that registers only ``tools/list`` and ``tools/call`` answers ``prompts/list``,
``resources/list`` and ``resources/templates/list`` with JSON-RPC -32601
"Method not found", as many real tools-only backends do. It advertises neither
capability, either.

Run as a script; it serves over stdio until the parent closes the pipes::

    python tests/support/tools_only_backend_server.py
"""

from __future__ import annotations

from typing import Any

import anyio
from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

ECHO = types.Tool(
    name="echo",
    description="Echo the given value back verbatim.",
    input_schema={
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    },
)


async def _list_tools(
    _ctx: ServerRequestContext[Any], _params: types.PaginatedRequestParams | None
) -> types.ListToolsResult:
    return types.ListToolsResult(tools=[ECHO])


async def _call_tool(
    _ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
) -> types.CallToolResult:
    value = (params.arguments or {})["value"]
    return types.CallToolResult(content=[types.TextContent(type="text", text=value)])


def build_server() -> Server[Any]:
    """Build the tools-only backend. Performs no I/O."""
    return Server(
        "tools-only-backend", on_list_tools=_list_tools, on_call_tool=_call_tool
    )


async def _serve_stdio() -> None:
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


if __name__ == "__main__":
    anyio.run(_serve_stdio)
