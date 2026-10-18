"""The proxy's answer to its clients' ``initialize`` and ``server/discover``.

Both are answered by the proxy itself, under its own server identity and
protocol version, then adjusted to present the backend as it is: the backend's
``instructions`` are passed on, and the prompts and resources capabilities are
advertised only when the backend advertised them. The backend's values come
from what it negotiated on its last successful connect (``BackendMetadata``),
never from a call to the backend, so these requests never fail on the backend's
account and add no traffic to its queue. Before the backend's first connect the
proxy's own answer goes out unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from mcp import types as mcp_types

from mcp_call_orchestrator_proxy.resilient import BackendMetadata


class BackendMetadataMiddleware(Middleware):
    """Applies the backend's negotiated metadata to the proxy's own answers."""

    def __init__(self, metadata: Callable[[], BackendMetadata | None]) -> None:
        self._metadata = metadata

    async def on_initialize(
        self,
        context: MiddlewareContext[mcp_types.InitializeRequest],
        call_next: CallNext[
            mcp_types.InitializeRequest, mcp_types.InitializeResult | None
        ],
    ) -> mcp_types.InitializeResult | None:
        result = await call_next(context)
        if result is None:
            return None
        return self._apply(result)

    async def on_discover(
        self,
        context: MiddlewareContext[mcp_types.DiscoverRequest],
        call_next: CallNext[
            mcp_types.DiscoverRequest, mcp_types.DiscoverResult | dict[str, Any]
        ],
    ) -> mcp_types.DiscoverResult | dict[str, Any]:
        result = await call_next(context)
        if not isinstance(result, mcp_types.DiscoverResult):
            return result
        return self._apply(result)

    def _apply[R: (mcp_types.InitializeResult, mcp_types.DiscoverResult)](
        self, result: R
    ) -> R:
        backend = self._metadata()
        if backend is None:
            return result
        update: dict[str, Any] = {
            "capabilities": _present_capabilities(
                result.capabilities, backend.capabilities
            )
        }
        if result.instructions is None and backend.instructions is not None:
            update["instructions"] = backend.instructions
        return result.model_copy(update=update)


def _present_capabilities(
    proxy: mcp_types.ServerCapabilities,
    backend: mcp_types.ServerCapabilities | None,
) -> mcp_types.ServerCapabilities:
    """The proxy's capabilities, less the prompts and resources the backend lacks.

    Tools stay advertised whatever the backend has, because ``backend_status``
    is always served. A capability the backend did not advertise would only
    send clients to discovery that the backend answers with "Method not found".
    """
    if backend is None:
        return proxy
    return proxy.model_copy(
        update={
            "prompts": proxy.prompts if backend.prompts is not None else None,
            "resources": proxy.resources if backend.resources is not None else None,
        }
    )
