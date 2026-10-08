"""Unit tests for the FastMCP proxy server construction.

These tests guard exact schema parity: every backend tool is exposed
with its own JSON schema, never behind a generic `arguments: dict`
wrapper. They drive the proxy through an in-memory `fastmcp.Client`,
exactly as a real MCP client would, and assert both the exposed schema
and the arguments that reach the backend are unchanged from the
backend's own.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client
from mcp import types as mcp_types

from mcp_call_orchestrator_proxy.app import create_proxy_app
from mcp_call_orchestrator_proxy.config import ProxySettings

VAULT_READ_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "File path relative to vault root",
        },
    },
    "required": ["path"],
}


class FakeBackend:
    """A minimal MCPBackendClient fake exposing one tool with a real schema."""

    def __init__(self) -> None:
        self.received_arguments: dict[str, Any] | None = None

    async def __aenter__(self) -> FakeBackend:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def initialize(self) -> mcp_types.InitializeResult:
        return mcp_types.InitializeResult(
            protocolVersion="2025-06-18",
            capabilities=mcp_types.ServerCapabilities(),
            serverInfo=mcp_types.Implementation(name="fake", version="0"),
        )

    async def ping(self) -> bool:
        return True

    async def list_tools(self) -> list[mcp_types.Tool]:
        return [
            mcp_types.Tool(
                name="vault_read",
                description="Read a vault file's content and metadata.",
                inputSchema=VAULT_READ_SCHEMA,
            )
        ]

    async def call_tool_mcp(
        self,
        name: str,
        arguments: dict[str, Any],
        meta: dict[str, Any] | None = None,
    ) -> mcp_types.CallToolResult:
        self.received_arguments = arguments
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="# Example")]
        )

    async def list_resources(self) -> list[mcp_types.Resource]:
        return []

    async def list_resource_templates(self) -> list[mcp_types.ResourceTemplate]:
        return []

    async def read_resource(self, uri: str) -> list[Any]:
        return []

    async def list_prompts(self) -> list[mcp_types.Prompt]:
        return []

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> mcp_types.GetPromptResult:
        return mcp_types.GetPromptResult(messages=[])


@pytest.fixture
def fake_settings() -> ProxySettings:
    return ProxySettings(backend_api_key="a" * 32)


@pytest.mark.asyncio
async def test_proxy_exposes_exact_upstream_schema(
    fake_settings: ProxySettings,
) -> None:
    fake_backend = FakeBackend()
    proxy_app = create_proxy_app(settings=fake_settings, backend_client=fake_backend)

    async with Client(proxy_app.mcp) as client:
        tools = await client.list_tools()

    # The proxy exposes every backend tool with its exact upstream schema, plus
    # the one intentional proxy-native tool (backend_status).
    by_name = {t.name for t in tools}
    assert by_name == {"vault_read", "backend_status"}

    vault_read = next(t for t in tools if t.name == "vault_read")
    assert vault_read.inputSchema == VAULT_READ_SCHEMA


@pytest.mark.asyncio
async def test_proxy_reports_its_configured_server_name() -> None:
    """The name a client sees in the `initialize` handshake comes from
    `ProxySettings.server_name`, not a hardcoded literal - this is what lets
    two proxy instances fronting different backends be told apart by a
    client that labels/dedupes servers by their reported identity (e.g. VS
    Code/Copilot's tool picker)."""
    settings = ProxySettings(backend_api_key="a" * 32, server_name="example-proxy")
    proxy_app = create_proxy_app(settings=settings, backend_client=FakeBackend())

    async with Client(proxy_app.mcp) as client:
        assert client.initialize_result is not None
        assert client.initialize_result.serverInfo.name == "example-proxy"


@pytest.mark.asyncio
async def test_proxy_calls_tool_with_its_own_advertised_parameter_names(
    fake_settings: ProxySettings,
) -> None:
    """Regression test: calling with `path="x"` must reach the backend as
    `{"path": "x"}`, not wrapped in a generic `arguments={...}` envelope."""
    fake_backend = FakeBackend()
    proxy_app = create_proxy_app(settings=fake_settings, backend_client=fake_backend)

    async with Client(proxy_app.mcp) as client:
        result = await client.call_tool("vault_read", {"path": "notes/todo.md"})

    assert fake_backend.received_arguments == {"path": "notes/todo.md"}
    assert not result.is_error
