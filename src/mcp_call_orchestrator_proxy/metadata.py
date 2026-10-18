"""The proxy's answer to its clients' ``initialize`` and ``server/discover``.

Both are answered by the proxy itself, under its own server identity and
protocol version, then adjusted to present the backend as it is: the backend's
``instructions`` are passed on, and a capability is advertised only when the
backend advertised it too, tools aside. The backend's values come
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
    """The proxy's capabilities, less each one the backend did not advertise.

    Tools stay advertised whatever the backend has, because ``backend_status``
    is always served. Within ``experimental`` and ``extensions`` an entry stays
    only when the backend has the same identifier. A kept capability keeps the
    proxy's own flags: they describe what the proxy delivers to its clients.
    """
    if backend is None:
        return proxy
    update: dict[str, Any] = {}
    for name in type(proxy).model_fields:
        ours, theirs = getattr(proxy, name), getattr(backend, name)
        if name == "tools" or ours is None:
            continue
        if isinstance(ours, dict):
            update[name] = {
                k: v for k, v in ours.items() if k in (theirs or {})
            } or None
        elif theirs is None:
            update[name] = None
    return proxy.model_copy(update=update)
