"""Unit tests for the FastMCP proxy server construction.

These tests guard exact schema parity: every backend tool is exposed
with its own JSON schema, never behind a generic `arguments: dict`
wrapper. They drive the proxy through an in-memory `fastmcp.Client`,
exactly as a real MCP client would, and assert both the exposed schema
and the arguments that reach the backend are unchanged from the
backend's own.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
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
        self.tool_name = "vault_read"

    async def __aenter__(self) -> FakeBackend:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    @property
    def protocol_version(self) -> str | None:
        return "2025-11-25"

    @property
    def server_capabilities(self) -> mcp_types.ServerCapabilities | None:
        return mcp_types.ServerCapabilities(tools=mcp_types.ToolsCapability())

    @property
    def instructions(self) -> str | None:
        return None

    async def list_tools(self) -> list[mcp_types.Tool]:
        return [
            mcp_types.Tool(
                name=self.tool_name,
                description="Read a vault file's content and metadata.",
                input_schema=VAULT_READ_SCHEMA,
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

    async def read_resource_mcp(
        self, uri: str, meta: dict[str, Any] | None = None
    ) -> mcp_types.ReadResourceResult:
        return mcp_types.ReadResourceResult(contents=[])

    async def list_prompts(self) -> list[mcp_types.Prompt]:
        return []

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        meta: dict[str, Any] | None = None,
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
    assert vault_read.input_schema == VAULT_READ_SCHEMA


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
        assert client.server_info is not None
        assert client.server_info.name == "example-proxy"


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


def _input_schema_lookup(proxy_app: Any) -> Any:
    lookup = proxy_app.mcp._mcp_server.get_tool_input_schema
    assert lookup is not None
    return lookup


@pytest.mark.asyncio
async def test_the_header_check_reads_schemas_from_the_served_listing(
    fake_settings: ProxySettings,
) -> None:
    """The schema lookup the SDK runs before a 2026-07-28 call answers from the
    listing the proxy served, with no backend call: nothing before a listing,
    then each listed tool's schema as served, ``backend_status`` included."""
    proxy_app = create_proxy_app(settings=fake_settings, backend_client=FakeBackend())
    lookup = _input_schema_lookup(proxy_app)

    async with Client(proxy_app.mcp) as client:
        assert lookup("vault_read") is None
        served = {t.name: t.input_schema for t in await client.list_tools()}

    assert lookup("vault_read") == served["vault_read"] == VAULT_READ_SCHEMA
    assert lookup("backend_status") == served["backend_status"]
    assert lookup("unknown") is None


@pytest.mark.asyncio
async def test_the_header_check_has_no_schema_for_a_hidden_tool() -> None:
    """A tool exposure control hides is absent from the listing, so the lookup
    has no schema for it either, as the backend-listing fallback had none."""
    settings = ProxySettings(backend_api_key="a" * 32, tool_deny=("vault_read",))
    proxy_app = create_proxy_app(settings=settings, backend_client=FakeBackend())
    lookup = _input_schema_lookup(proxy_app)

    async with Client(proxy_app.mcp) as client:
        assert {t.name for t in await client.list_tools()} == {"backend_status"}

    assert lookup("vault_read") is None
    assert lookup("backend_status") is not None


@pytest.mark.asyncio
async def test_a_tool_is_looked_up_in_the_listing_last_served(
    fake_settings: ProxySettings,
    age_cached_listings: Callable[[FastMCP, float], None],
) -> None:
    """A lookup resolves from the backend's tools as a client was last served
    them, however long ago, and follows the next listing a client is served."""
    fake_backend = FakeBackend()
    proxy_app = create_proxy_app(settings=fake_settings, backend_client=fake_backend)

    async with Client(proxy_app.mcp) as client:
        await client.list_tools()
        fake_backend.tool_name = "vault_read_v2"
        age_cached_listings(proxy_app.mcp, 301.0)

        result = await client.call_tool("vault_read", {"path": "a.md"})
        assert not result.is_error
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool("vault_read_v2", {"path": "a.md"})

        assert "vault_read_v2" in {t.name for t in await client.list_tools()}
        result = await client.call_tool("vault_read_v2", {"path": "b.md"})
        assert not result.is_error
        assert fake_backend.received_arguments == {"path": "b.md"}
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool("vault_read", {"path": "a.md"})
