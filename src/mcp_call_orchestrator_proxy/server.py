"""FastMCP server construction for the proxy.

A plain `FastMCP` server with FastMCP's `ProxyProvider` over the queue-aware
backend facade, which mirrors upstream tools/resources/prompts using their
exact original JSON schema - no lossy `arguments: dict` wrapper - and the
proxy's own `BackendMetadataMiddleware` for `initialize` and `server/discover`.
A 2026-07-28 call's `Mcp-Param-*` headers are checked against the tool list the
proxy last served (`ListedToolSchemas`). A tool call, resource read or prompt
request looks its item up in the backend listing `ProxyProvider` last served for
that surface, however old; only a lookup made before any listing of its surface
lists the backend.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, cast

from fastmcp import Client, FastMCP
from fastmcp.server.providers.proxy import ProxyProvider

from mcp_call_orchestrator_proxy.interfaces import MCPBackendClient
from mcp_call_orchestrator_proxy.metadata import BackendMetadataMiddleware
from mcp_call_orchestrator_proxy.resilient import BackendMetadata
from mcp_call_orchestrator_proxy.tool_schemas import ListedToolSchemas


def build_proxy_server(
    backend_factory: Callable[[], MCPBackendClient],
    *,
    metadata: Callable[[], BackendMetadata | None],
    server_name: str = "mcp-call-orchestrator-proxy",
    lifespan: Callable[[FastMCP], AbstractAsyncContextManager[None]] | None = None,
) -> FastMCP:
    """Create a FastMCP server that mirrors the backend's exact tool schemas.

    Not `FastMCPProxy` and not `create_proxy()`: `create_proxy()` treats a
    non-`Client` target as a raw transport to wrap in a fresh `ProxyClient`,
    and `FastMCPProxy` installs `ProxyMetadataMiddleware`, which reads backend
    client internals and calls the backend on every client handshake, outside
    the proxy's queue. `ProxyProvider` relies on the facade only structurally
    (duck-typed against `Client`), never through an `isinstance` check.

    `metadata` returns what the backend last negotiated; it is read locally on
    each client handshake and never calls the backend.
    """
    listed = ListedToolSchemas()
    server = FastMCP(
        name=server_name,
        lifespan=lifespan,
        middleware=[BackendMetadataMiddleware(metadata), listed],
        # FastMCP inlines `$ref`/`$defs` by default; schemas pass through as the
        # backend wrote them, so their structure stays the backend's own.
        dereference_schemas=False,
    )
    # A down backend's listing failure is logged and skipped (FastMCP's "warn"
    # default for providers), so the listing still carries `backend_status`.
    # Lookups resolve from the listing a client was last served, however old:
    # an expired cache would have a lookup list the backend through the queue,
    # ahead of the call and under the calling client's name.
    server.add_provider(
        ProxyProvider(
            cast(Callable[[], Client[Any]], backend_factory), cache_ttl=math.inf
        )
    )
    # The SDK's streamable-HTTP server reads this lookup off the low-level server,
    # which FastMCP leaves unset; without it the header check lists the backend.
    server._mcp_server.get_tool_input_schema = listed.input_schema
    return server
