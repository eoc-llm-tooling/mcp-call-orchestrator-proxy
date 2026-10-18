"""Factory for the real backend connection.

fastmcp's own `Client` already handles Bearer auth, self-signed TLS, and
reentrant session lifecycle - no hand-rolled httpx/ClientSession plumbing
needed here.

Two backend sources are supported (see `ProxySettings`):

- A standard ``mcpServers`` JSON config file (`backend_config`). FastMCP's
  ``MCPConfig`` parses and validates it; a single-server config yields a direct
  transport with no tool-name prefixing, preserving exact tool parity. TLS
  verification is applied on top from `backend_verify_tls`, since the standard
  format has no verify field.
- The individual `backend_mcp_url` + `backend_api_key` fields (fallback).

A config that does not define exactly one server, and FastMCP's transform keys
(`tools`, `include_tags`, `exclude_tags`), are refused at startup
(`check_backend_config`). FastMCP applies the transform keys through a nested
in-process proxy around the real transport, out of reach of the `keep_alive`
and TLS settings this module applies.

The client relays tool results without checking them against the tool's
declared output schema (`RelayClientSession`); the proxy's own clients check
what they receive.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
from fastmcp import Client
from fastmcp.client.transports import (
    ClientTransport,
    SSETransport,
    StdioTransport,
    StreamableHttpTransport,
)
from fastmcp.client.transports.base import TransportOptions
from fastmcp.mcp_config import MCPConfig, RemoteMCPServer, StdioMCPServer
from mcp import types as mcp_types
from mcp.client.session import ClientSession

from mcp_call_orchestrator_proxy.config import ProxySettings

_UNSUPPORTED_ENTRY_KEYS = ("tools", "include_tags", "exclude_tags")

# How much longer the backend client's own timeouts - the MCP request's and the
# HTTP read's - run than a call may take, so the queue's is the one that fires.
_BACKSTOP_GRACE_SECONDS = 5.0


class RelayClientSession(ClientSession):
    """A backend session that returns tool results as the backend sent them.

    `ClientSession` checks a tool's structured content against the tool's
    declared output schema and raises `RuntimeError` on a mismatch - the same
    type FastMCP raises for a client that is not connected, which
    `ResilientBackend` treats as a broken connection. The proxy relays results
    rather than consuming them, and each of its clients runs the check itself.
    """

    async def validate_tool_result(
        self, name: str, result: mcp_types.CallToolResult
    ) -> None:
        return None


_RELAY_OPTIONS = TransportOptions(session_class=RelayClientSession)


def build_real_backend_client(settings: ProxySettings) -> Client[Any]:
    """Build a fastmcp Client for the configured backend.

    A plain string `auth` is auto-wrapped as Bearer auth by fastmcp;
    `verify=False` disables TLS verification for a self-signed cert.
    `init_timeout` bounds a single connect/initialize attempt so the resilient
    supervisor's retry loop stays responsive when the backend is slow or absent.
    The queue's `call_timeout_seconds` bounds each call, and a call that
    exceeds it fails alone; the client's own MCP request `timeout` is a
    backstop, `_BACKSTOP_GRACE_SECONDS` longer. Of equal length, it could fire
    first and fail the call with an `MCPError` that says nothing of the
    timeout's length. Tool results are not checked against their output
    schema (`RelayClientSession`).
    """
    request_timeout = settings.call_timeout_seconds + _BACKSTOP_GRACE_SECONDS
    client: Client[Any]
    if settings.backend_config:
        transport = _single_server_transport(settings.backend_config)
        kwargs: dict[str, Any] = {
            "timeout": request_timeout,
            "init_timeout": settings.connect_timeout_seconds,
        }
        # `verify` only applies to HTTP transports; a stdio backend has no TLS.
        if isinstance(transport, StreamableHttpTransport | SSETransport):
            kwargs["verify"] = settings.backend_verify_tls
        client = Client(transport, **kwargs)
    else:
        client = Client(
            settings.backend_mcp_url,
            auth=settings.backend_api_key,
            verify=settings.backend_verify_tls,
            timeout=request_timeout,
            init_timeout=settings.connect_timeout_seconds,
        )
    if isinstance(client.transport, StreamableHttpTransport):
        client.transport.httpx_client_factory = _http_client_factory(
            verify=settings.backend_verify_tls,
            read_timeout=request_timeout,
        )
    # `Client` takes its session class only from these options, and has no
    # constructor argument for them; FastMCP's own `ProxyClient` sets them so.
    client._transport_options = _RELAY_OPTIONS
    return client


def _http_client_factory(
    *, verify: bool, read_timeout: float
) -> Callable[..., httpx2.AsyncClient]:
    """Build the backend's HTTP client with a read timeout longer than a call.

    A call that outlasts its timeout is cancelled by the queue, and the MCP SDK
    then abandons that one request, aborting its HTTP request on a 2026-07-28
    connection. A read timeout that fires first on a 2026-07-28 connection,
    whose answers are not streamed, makes the SDK's transport fail as a whole
    and end the connection under every call queued behind the hung one; so
    the read timeout is the MCP request timeout, longer than a call's. FastMCP
    ignores `verify` once a factory is set, so the factory applies it.
    """

    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx2.Timeout | None = None,
        auth: httpx2.Auth | None = None,
        **kwargs: Any,
    ) -> httpx2.AsyncClient:
        base = timeout or httpx2.Timeout(30.0)
        return httpx2.AsyncClient(
            headers=headers,
            auth=auth,
            verify=verify,
            timeout=httpx2.Timeout(
                connect=base.connect,
                read=read_timeout,
                write=base.write,
                pool=base.pool,
            ),
            **kwargs,
        )

    return factory


def _single_server_transport(config_path: str) -> ClientTransport:
    """Load a standard mcpServers JSON file and return its single transport.

    This proxy fronts exactly one backend, so a config that defines more than
    one server is rejected: FastMCP would otherwise mount them as a composite
    and prefix every tool name (`{server}_{tool}`), breaking exact parity.
    """
    config = MCPConfig.from_file(Path(config_path))
    _require_single_server(config, config_path)
    server = next(iter(config.mcpServers.values()))
    # The plain type's `to_transport`, never a transforming subclass's override:
    # that one returns a nested proxy, which the settings below cannot reach.
    transport: ClientTransport
    if isinstance(server, StdioMCPServer):
        transport = StdioMCPServer.to_transport(server)
    else:
        transport = RemoteMCPServer.to_transport(server)
    if isinstance(transport, StdioTransport):
        # `keep_alive=True` (fastmcp's default) is for reusing one subprocess
        # across several separate `async with client:` blocks in a long-lived
        # host process - not this proxy's shape. ResilientBackend builds a
        # fresh client per connection attempt and lets the old one go out of
        # scope on disconnect; with keep_alive left on, that leaves the
        # subprocess running until Python happens to garbage-collect it. This
        # proxy owns a stdio backend's whole life (docs/roadmap.md), so the
        # override is unconditional - regardless of what the config's own
        # `keep_alive` field says - to guarantee no orphaned child survives a
        # reconnect or a shutdown.
        transport.keep_alive = False
    return transport


def _require_single_server(config: MCPConfig, config_path: str) -> None:
    servers = config.mcpServers
    if len(servers) != 1:
        raise ValueError(
            f"Backend config {config_path!r} must define exactly one MCP server "
            f"(this proxy fronts a single backend); found {len(servers)}: "
            f"{sorted(servers)}."
        )


def check_backend_config(settings: ProxySettings) -> None:
    """Refuse a backend config this proxy cannot honour.

    Called once at startup, so the proxy does not start on such a config. It
    refuses a config that does not define exactly one server, and an entry
    that carries a FastMCP transform key. A key is refused whatever its value:
    FastMCP reads an empty one as absent, but the operator still wrote it.
    """
    if not settings.backend_config:
        return
    config = MCPConfig.from_file(Path(settings.backend_config))
    _require_single_server(config, settings.backend_config)
    for name, server in config.mcpServers.items():
        given = server.model_fields_set | set(server.model_extra or {})
        refused = [key for key in _UNSUPPORTED_ENTRY_KEYS if key in given]
        if refused:
            keys = ", ".join(repr(key) for key in refused)
            them = "it" if len(refused) == 1 else "them"
            raise ValueError(
                f"Backend config {settings.backend_config!r}: server {name!r} sets "
                f"{keys}, which this proxy does not apply: FastMCP implements "
                f"{them} with a nested proxy that bypasses this proxy's transport "
                f"settings. Remove {them} from the entry."
            )


def resolve_backend_url(settings: ProxySettings) -> str:
    """Human-readable backend URL for the `backend_status` tool.

    In config-file mode the URL lives inside the JSON; fall back to a stdio
    marker for a command-based backend that has no URL.
    """
    if settings.backend_config:
        config = MCPConfig.from_file(Path(settings.backend_config))
        server = next(iter(config.mcpServers.values()))
        return str(getattr(server, "url", None) or f"<stdio:{settings.backend_config}>")
    return settings.backend_mcp_url
