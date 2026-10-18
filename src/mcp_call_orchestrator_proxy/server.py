"""FastMCP server construction for the proxy.

A plain `FastMCP` server with FastMCP's `ProxyProvider` over the queue-aware
backend facade, which mirrors upstream tools/resources/prompts using their
exact original JSON schema - no lossy `arguments: dict` wrapper - and the
proxy's own `BackendMetadataMiddleware` for `initialize` and `server/discover`.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, cast

from fastmcp import Client, FastMCP
from fastmcp.server.providers.proxy import ProxyProvider

from mcp_call_orchestrator_proxy.interfaces import MCPBackendClient
from mcp_call_orchestrator_proxy.metadata import BackendMetadataMiddleware
from mcp_call_orchestrator_proxy.resilient import BackendMetadata


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
    server = FastMCP(
        name=server_name,
        lifespan=lifespan,
        middleware=[BackendMetadataMiddleware(metadata)],
        # FastMCP inlines `$ref`/`$defs` by default; schemas pass through as the
        # backend wrote them, so their structure stays the backend's own.
        dereference_schemas=False,
    )
    # A down backend's listing failure is logged and skipped (FastMCP's "warn"
    # default for providers), so the listing still carries `backend_status`.
    server.add_provider(ProxyProvider(cast(Callable[[], Client[Any]], backend_factory)))
    return server
