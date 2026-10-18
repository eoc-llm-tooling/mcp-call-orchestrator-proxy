"""Exact schema parity over a real HTTP transport for schemas that use ``$ref``.

A schema with ``$ref`` is the case where a server can change a schema's structure
without changing what it accepts: inlining the referenced subschema and dropping
``$defs``. The proxy passes such schemas through as the backend wrote them, so a
client comparing schemas sees no difference from the backend's own.

An in-process mock supplies the schemas, so the test controls exactly what the
backend declares: one ``$ref`` into ``$defs``, and one internal JSON pointer of
the kind some schema generators emit for a repeated subschema.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from fastmcp import Client
from mcp import types as mcp_types

from mcp_call_orchestrator_proxy.app import create_proxy_app
from mcp_call_orchestrator_proxy.config import ProxySettings

pytestmark = pytest.mark.component

DEFS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"pet": {"$ref": "#/$defs/Cat"}},
    "required": ["pet"],
    "$defs": {
        "Cat": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
}

INTERNAL_POINTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "destination": {
            "type": "object",
            "properties": {
                "parent": {
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {"id": {"type": "string"}},
                            "required": ["id"],
                        },
                        {"type": "null"},
                    ],
                },
            },
        },
        "source": {"$ref": "#/properties/destination/properties/parent/anyOf/0"},
    },
}

BACKEND_SCHEMAS = {
    "adopt": DEFS_SCHEMA,
    "move": INTERNAL_POINTER_SCHEMA,
}


class RefSchemaBackend:
    """A mock ``MCPBackendClient`` whose tools declare schemas that use ``$ref``.

    Hands out a fresh copy of each schema per listing, so a layer that edits a
    schema in place cannot also edit the expected value the test compares with.
    """

    async def __aenter__(self) -> RefSchemaBackend:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    @property
    def protocol_version(self) -> str | None:
        return "2025-11-25"

    @property
    def server_info(self) -> mcp_types.Implementation | None:
        return mcp_types.Implementation(name="ref-schemas", version="0")

    @property
    def server_capabilities(self) -> mcp_types.ServerCapabilities | None:
        return mcp_types.ServerCapabilities(tools=mcp_types.ToolsCapability())

    @property
    def instructions(self) -> str | None:
        return None

    async def list_tools(self) -> list[mcp_types.Tool]:
        return [
            mcp_types.Tool(
                name=name,
                description=f"Tool {name}.",
                input_schema=copy.deepcopy(schema),
            )
            for name, schema in BACKEND_SCHEMAS.items()
        ]

    async def call_tool_mcp(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        meta: dict[str, Any] | None = None,
    ) -> mcp_types.CallToolResult:
        return mcp_types.CallToolResult(content=[])

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


@pytest.mark.asyncio
async def test_schemas_with_ref_reach_clients_unchanged(serve_over_http) -> None:
    """A ``$defs`` schema and an internal-pointer schema are listed through the
    proxy exactly as the backend declares them: ``$ref`` kept, ``$defs`` kept."""
    proxy_app = create_proxy_app(
        settings=ProxySettings(backend_api_key="a" * 32),
        backend_client=RefSchemaBackend(),
    )

    async with serve_over_http(proxy_app.mcp) as url, Client(url) as client:
        listed = {tool.name: tool.input_schema for tool in await client.list_tools()}

    assert {name: listed[name] for name in BACKEND_SCHEMAS} == BACKEND_SCHEMAS
