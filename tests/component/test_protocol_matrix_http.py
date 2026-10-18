"""Every pairing of client and backend protocol version, through the proxy.

MCP has two kinds of protocol version: those up to 2025-11-25 open a session
with the ``initialize`` handshake, and 2026-07-28 has no session and starts with
``server/discover``. The proxy sits between a client and a backend that may each
use either. Here a real client of each kind talks to the proxy over real HTTP,
and the proxy reaches a real backend of each kind (``probe_backend_server.py``,
with ``--legacy`` for the handshake-only kind) through its production
``client.py`` path, negotiating the backend's version as the MCP library does by
default.

Each pairing must complete discovery and a tool call, with the backend's
instructions reaching the client (#6). The proxy answers the client's handshake
itself - from what the backend last negotiated - so the handshake neither fails
nor puts a call on the backend's queue, even with the backend down.
"""

from __future__ import annotations

import logging
import socket
import sys
from pathlib import Path

import pytest
from fastmcp import Client

from mcp_call_orchestrator_proxy.app import create_proxy_app
from mcp_call_orchestrator_proxy.config import ProxySettings

sys.path.insert(0, str(Path(__file__).parent.parent / "support"))
from probe_backend_server import INSTRUCTIONS

pytestmark = pytest.mark.component

UI = "io.modelcontextprotocol/ui"
HANDSHAKE = "2025-11-25"
SESSIONLESS = "2026-07-28"

# Client mode -> the version the client ends up speaking with the proxy. "auto"
# asks the proxy with `server/discover`; a pinned version would not ask at all.
CLIENT_MODES = {"legacy": HANDSHAKE, "auto": SESSIONLESS}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend_legacy", [True, False], ids=["backend-2025", "backend-2026"]
)
@pytest.mark.parametrize(
    "client_mode", list(CLIENT_MODES), ids=["client-2025", "client-2026"]
)
async def test_each_pairing_completes_discovery_and_a_tool_call(
    spawn_backend,
    backend_config_file,
    serve_over_http,
    client_mode: str,
    backend_legacy: bool,
) -> None:
    backend = spawn_backend(legacy=backend_legacy)
    proxy_app = create_proxy_app(
        settings=ProxySettings(backend_config=backend_config_file(backend.url))
    )

    async with (
        serve_over_http(proxy_app.mcp) as url,
        Client(url, mode=client_mode) as client,
    ):
        assert client.protocol_version == CLIENT_MODES[client_mode]
        assert client.instructions == INSTRUCTIONS
        # Control for the tools-only backend: what this backend has is offered.
        capabilities = client.server_capabilities
        assert capabilities is not None
        assert capabilities.prompts is not None
        assert capabilities.resources is not None
        assert capabilities.logging is not None
        # Extensions exist only from 2026-07-28, so only a 2026-07-28 backend has
        # one to pass on, and only a 2026-07-28 client is told of it.
        ui_offered = client_mode == "auto" and not backend_legacy
        assert (capabilities.extensions or {}).keys() == ({UI} if ui_offered else set())

        tools = {tool.name for tool in await client.list_tools()}
        assert {"echo", "slow_echo", "backend_status"} <= tools
        assert await client.list_prompts() == []
        assert await client.list_resources() == []

        result = await client.call_tool("echo", {"value": "through the proxy"})
        assert result.content[0].text == "through the proxy"

    metadata = proxy_app.backend_client.metadata
    assert metadata is not None
    assert metadata.protocol_version == (HANDSHAKE if backend_legacy else SESSIONLESS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client_mode", list(CLIENT_MODES), ids=["client-2025", "client-2026"]
)
async def test_the_handshake_puts_nothing_on_the_backend_queue(
    spawn_backend,
    backend_config_file,
    serve_over_http,
    caplog: pytest.LogCaptureFixture,
    client_mode: str,
) -> None:
    """``initialize`` and ``server/discover`` are answered by the proxy itself."""
    backend = spawn_backend()
    proxy_app = create_proxy_app(
        settings=ProxySettings(backend_config=backend_config_file(backend.url))
    )
    caplog.set_level(logging.INFO, logger="mcp_call_orchestrator_proxy")

    async with serve_over_http(proxy_app.mcp) as url:
        caplog.clear()
        async with Client(url, mode=client_mode) as client:
            assert client.instructions == INSTRUCTIONS
            queued = [r for r in caplog.records if "call accepted" in r.getMessage()]
            assert queued == [], [r.getMessage() for r in queued]
            # Control: the same log does show a call that does reach the backend.
            await client.list_tools()
            assert any("call accepted" in r.getMessage() for r in caplog.records)


def _unused_loopback_url() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/mcp/"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client_mode", list(CLIENT_MODES), ids=["client-2025", "client-2026"]
)
async def test_the_handshake_succeeds_with_the_backend_down(
    backend_config_file, serve_over_http, client_mode: str
) -> None:
    proxy_app = create_proxy_app(
        settings=ProxySettings(
            backend_config=backend_config_file(_unused_loopback_url()),
            connect_timeout_seconds=1.0,
            reconnect_initial_backoff_seconds=0.05,
            reconnect_max_backoff_seconds=0.2,
        )
    )

    async with (
        serve_over_http(proxy_app.mcp) as url,
        Client(url, mode=client_mode) as client,
    ):
        assert client.protocol_version == CLIENT_MODES[client_mode]
        # Nothing was ever negotiated with the backend, so there is nothing to pass on.
        assert client.instructions is None
        tools = {tool.name for tool in await client.list_tools()}
        assert tools == {"backend_status"}
