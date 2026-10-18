"""A real MCP server that implements tools and nothing else, served over stdio.

Built on the MCP SDK's low-level ``Server`` rather than FastMCP: a FastMCP server
always answers prompt and resource listings (with empty lists), while a server
that registers only ``tools/list`` and ``tools/call`` answers ``prompts/list``,
``resources/list`` and ``resources/templates/list`` with JSON-RPC -32601
"Method not found", as many real tools-only backends do.

Run as a script; it serves over stdio until the parent closes the pipes::

    python tests/support/tools_only_backend_server.py
"""

from __future__ import annotations

from typing import Any

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


def build_server() -> Server[Any]:
    """Build the tools-only backend. Performs no I/O."""
    server: Server[Any] = Server("tools-only-backend")

    @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="echo",
                description="Echo the given value back verbatim.",
                inputSchema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            )
        ]

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.Content]:
        return [types.TextContent(type="text", text=str(arguments["value"]))]

    return server


async def _serve_stdio() -> None:
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


if __name__ == "__main__":
    anyio.run(_serve_stdio)
