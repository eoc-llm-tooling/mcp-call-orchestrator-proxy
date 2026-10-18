"""Queue-aware facade over the real MCP backend connection.

Wraps a real `MCPBackendClient` so that every operation FastMCP's proxy
layer performs against it is routed through a `CallOrchestrator`,
serializing (or bounding the concurrency of) calls to the real backend.

FastMCP opens `async with client:` around every single backend
touchpoint (tool calls, all discovery, resource reads, prompt renders).
This facade's own `__aenter__`/`__aexit__` are no-ops: the one real
connection is opened and closed exactly once, for the lifetime of the app,
via the composition root's lifespan hook - not per call.

This is also where a call is attributed to the client that made it: naming
the caller means reading MCP's request context, which is knowledge the
generic queue below deliberately does not have. The label is handed down to
`CallOrchestrator.submit` as an opaque string.
"""

from __future__ import annotations

import itertools
import weakref
from typing import Any

from fastmcp.server.dependencies import get_context
from mcp import types as mcp_types
from mcp.types.version import MODERN_PROTOCOL_VERSIONS

from mcp_call_orchestrator_proxy.interfaces import MCPBackendClient
from mcp_call_orchestrator_proxy.orchestrator import CallOrchestrator

_UNNAMED_CLIENT = "client"
_PROXY_CALLER = "proxy"


class QueuedMCPBackend:
    """Routes every `MCPBackendClient` call through a `CallOrchestrator`."""

    def __init__(
        self, upstream: MCPBackendClient, orchestrator: CallOrchestrator
    ) -> None:
        self._upstream = upstream
        self._orchestrator = orchestrator

        # Session -> log tag, keyed by the identity of the session's `initialize`
        # params: the one object that lives exactly as long as the session (the
        # SDK builds a new session wrapper per request). The entry is dropped when
        # that object is collected, so its id() can never be reused for another
        # session while the entry exists; the counter is what keeps a tag unique.
        self._session_tags: dict[int, str] = {}
        self._session_seq = itertools.count(1)

    @property
    def protocol_version(self) -> str | None:
        """``None``: see ``MCPBackendClient.protocol_version``."""
        return None

    def _client_label(self) -> str:
        """Name the client whose request is being served, for log attribution.

        The name is the one the client gives in ``clientInfo``, or ``client``.
        Before 2026-07-28 it is followed by a number per session, taken at
        ``initialize``. From 2026-07-28 on there is no session, so two instances
        of one client log under the same name. A call with no client request
        behind it is the proxy's own and is tagged ``proxy``.
        """
        try:
            request = get_context().request_context
        except RuntimeError:
            request = None
        if request is None:
            return _PROXY_CALLER

        params = request.session.client_params
        name = (params.client_info.name if params else None) or _UNNAMED_CLIENT
        if params is None or request.protocol_version in MODERN_PROTOCOL_VERSIONS:
            return name

        key = id(params)
        tag = self._session_tags.get(key)
        if tag is None:
            tag = f"{name}-{next(self._session_seq):03d}"
            self._session_tags[key] = tag
            weakref.finalize(params, self._session_tags.pop, key, None)
        return tag

    async def __aenter__(self) -> QueuedMCPBackend:
        return self

    async def __aexit__(
        self, exc_type: object, exc_val: object, exc_tb: object
    ) -> None:
        return None

    async def list_tools(self) -> list[mcp_types.Tool]:
        return await self._orchestrator.submit(
            self._upstream.list_tools,
            label="list_tools",
            client=self._client_label(),
        )

    async def call_tool_mcp(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        meta: dict[str, Any] | None = None,
    ) -> mcp_types.CallToolResult:
        return await self._orchestrator.submit(
            lambda: self._upstream.call_tool_mcp(name, arguments, meta=meta),
            label=f"call_tool:{name}",
            client=self._client_label(),
        )

    async def list_resources(self) -> list[mcp_types.Resource]:
        return await self._orchestrator.submit(
            self._upstream.list_resources,
            label="list_resources",
            client=self._client_label(),
        )

    async def list_resource_templates(self) -> list[mcp_types.ResourceTemplate]:
        return await self._orchestrator.submit(
            self._upstream.list_resource_templates,
            label="list_resource_templates",
            client=self._client_label(),
        )

    async def read_resource_mcp(
        self, uri: str, meta: dict[str, Any] | None = None
    ) -> mcp_types.ReadResourceResult:
        return await self._orchestrator.submit(
            lambda: self._upstream.read_resource_mcp(uri, meta=meta),
            label="read_resource",
            client=self._client_label(),
        )

    async def list_prompts(self) -> list[mcp_types.Prompt]:
        return await self._orchestrator.submit(
            self._upstream.list_prompts,
            label="list_prompts",
            client=self._client_label(),
        )

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        meta: dict[str, Any] | None = None,
    ) -> mcp_types.GetPromptResult:
        return await self._orchestrator.submit(
            lambda: self._upstream.get_prompt(name, arguments, meta=meta),
            label=f"get_prompt:{name}",
            client=self._client_label(),
        )
