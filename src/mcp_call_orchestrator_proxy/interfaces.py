"""Core interfaces (Protocols) to enable dependency inversion and testability.

Decouples the queuing facade from the concrete MCP client implementation
(a real `fastmcp.Client` or a test fake).
"""

from __future__ import annotations

from typing import Any, Protocol

from mcp import types as mcp_types


class MCPBackendClient(Protocol):
    """Structural protocol for the backend object FastMCP's proxy provider drives.

    Mirrors the subset of `fastmcp.Client`'s public surface that
    `fastmcp.server.providers.proxy` calls on its request/response path:
    discovery, tool calls, resource reads and prompt renders. A real `Client`
    satisfies this structurally; no adapter is required.
    """

    async def __aenter__(self) -> MCPBackendClient: ...

    async def __aexit__(
        self, exc_type: object, exc_val: object, exc_tb: object
    ) -> None: ...

    @property
    def protocol_version(self) -> str | None:
        """The protocol version FastMCP's proxy provider branches on.

        For a 2026-07-28+ version the provider calls the backend's session
        directly instead of the methods below, which would bypass every layer
        under it. Each of the proxy's own layers therefore reports ``None``; the
        version actually negotiated with the backend is in
        ``ResilientBackend.metadata``.
        """
        ...

    async def list_tools(self) -> list[mcp_types.Tool]:
        """Return the list of available tools exactly as discovered upstream."""
        ...

    async def call_tool_mcp(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        meta: dict[str, Any] | None = None,
    ) -> mcp_types.CallToolResult:
        """Execute a tool call and return the raw MCP protocol result."""
        ...

    async def list_resources(self) -> list[mcp_types.Resource]:
        """Return the list of available resources."""
        ...

    async def list_resource_templates(self) -> list[mcp_types.ResourceTemplate]:
        """Return the list of available resource templates."""
        ...

    async def read_resource_mcp(
        self, uri: str, meta: dict[str, Any] | None = None
    ) -> mcp_types.ReadResourceResult:
        """Read a resource and return the raw MCP protocol result."""
        ...

    async def list_prompts(self) -> list[mcp_types.Prompt]:
        """Return the list of available prompts."""
        ...

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        meta: dict[str, Any] | None = None,
    ) -> mcp_types.GetPromptResult:
        """Render a prompt."""
        ...


class NegotiatedBackendClient(MCPBackendClient, Protocol):
    """A connected backend client that also reports what it negotiated.

    The era-neutral negotiation result a connected `fastmcp.Client` exposes,
    populated from the `initialize` handshake (protocol versions up to
    2025-11-25) or from `server/discover` (2026-07-28 and later). Each value is
    ``None`` while disconnected. This is what `ResilientBackend` builds per
    connection.
    """

    @property
    def server_info(self) -> mcp_types.Implementation | None:
        """The backend's server identity."""
        ...

    @property
    def server_capabilities(self) -> mcp_types.ServerCapabilities | None:
        """The capabilities the backend advertised."""
        ...

    @property
    def instructions(self) -> str | None:
        """The backend's server instructions."""
        ...
