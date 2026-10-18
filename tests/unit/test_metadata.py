"""Unit tests for the proxy's answer to ``initialize`` and ``server/discover``.

The backend here is a real FastMCP server reached in-process, so the
instructions and capabilities the proxy passes on are the ones a real backend
negotiates; the client speaks either protocol version.
"""

from __future__ import annotations

import pytest
from fastmcp import Client, FastMCP
from mcp import types as mcp_types

from mcp_call_orchestrator_proxy.app import create_proxy_app
from mcp_call_orchestrator_proxy.config import ProxySettings
from mcp_call_orchestrator_proxy.metadata import _present_capabilities

CLIENT_MODES = ["legacy", "auto"]


def _backend(instructions: str | None) -> FastMCP:
    backend = FastMCP("backend", instructions=instructions)

    @backend.tool
    def echo(value: str) -> str:
        return value

    return backend


@pytest.mark.asyncio
@pytest.mark.parametrize("client_mode", CLIENT_MODES)
async def test_the_backends_instructions_reach_the_client(client_mode: str) -> None:
    backend = _backend("BACKEND INSTRUCTIONS")
    app = create_proxy_app(
        settings=ProxySettings(backend_api_key="a" * 32),
        backend_factory=lambda: Client(backend),
    )

    async with Client(app.mcp, mode=client_mode) as client:
        assert client.instructions == "BACKEND INSTRUCTIONS"


@pytest.mark.asyncio
@pytest.mark.parametrize("client_mode", CLIENT_MODES)
async def test_a_backend_without_instructions_gives_the_client_none(
    client_mode: str,
) -> None:
    backend = _backend(None)
    app = create_proxy_app(
        settings=ProxySettings(backend_api_key="a" * 32),
        backend_factory=lambda: Client(backend),
    )

    async with Client(app.mcp, mode=client_mode) as client:
        assert client.instructions is None
        assert client.server_info is not None
        assert client.server_info.name == "mcp-call-orchestrator-proxy"


UI = "io.modelcontextprotocol/ui"

PROXY_CAPABILITIES = mcp_types.ServerCapabilities(
    tools=mcp_types.ToolsCapability(list_changed=True),
    prompts=mcp_types.PromptsCapability(list_changed=True),
    resources=mcp_types.ResourcesCapability(list_changed=True),
    logging=mcp_types.LoggingCapability(),
    extensions={UI: {}},
)


def test_capabilities_are_the_proxys_own_until_the_backend_is_known() -> None:
    assert _present_capabilities(PROXY_CAPABILITIES, None) == PROXY_CAPABILITIES


def test_capabilities_the_backend_lacks_are_dropped() -> None:
    tools_only = mcp_types.ServerCapabilities(tools=mcp_types.ToolsCapability())

    presented = _present_capabilities(PROXY_CAPABILITIES, tools_only)

    assert presented == mcp_types.ServerCapabilities(tools=PROXY_CAPABILITIES.tools)


def test_only_the_extensions_the_backend_has_are_kept() -> None:
    proxy = PROXY_CAPABILITIES.model_copy(
        update={"extensions": {UI: {}, "example.org/other": {}}}
    )
    backend = mcp_types.ServerCapabilities(extensions={UI: {"backend": True}})

    assert _present_capabilities(proxy, backend).extensions == {UI: {}}
    assert (
        _present_capabilities(proxy, mcp_types.ServerCapabilities()).extensions is None
    )


def test_capabilities_the_backend_has_keep_the_proxys_own_flags() -> None:
    """The flags describe what the proxy delivers to its clients, not the backend."""
    backend = mcp_types.ServerCapabilities(
        prompts=mcp_types.PromptsCapability(list_changed=False),
        resources=mcp_types.ResourcesCapability(subscribe=True),
        logging=mcp_types.LoggingCapability(),
        extensions={UI: {"backend": True}},
    )

    assert _present_capabilities(PROXY_CAPABILITIES, backend) == PROXY_CAPABILITIES


def test_a_backend_without_tools_still_gets_tools_advertised() -> None:
    """``backend_status`` is a tool the proxy always serves."""
    backend = mcp_types.ServerCapabilities()

    assert _present_capabilities(PROXY_CAPABILITIES, backend).tools is not None
