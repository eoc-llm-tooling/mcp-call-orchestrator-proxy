"""End-to-end resilience proof over a real HTTP transport, with a mock backend.

The headline resilience promise, tested offline: the proxy starts and stays
serving even when Backend is absent, an agent can connect and ask
``backend_status`` without a failing call, and once the backend appears the
proxy reconnects on its own and tool calls succeed - all with no restart.

A ``GatedEchoBackend`` stands in for Backend: while ``gate["up"]`` is False its
``__aenter__`` raises (as fastmcp's real Client does when the endpoint is down),
so every connection attempt fails; flipping the gate lets it connect and serve a
single ``echo`` tool. Driving this over the real uvicorn HTTP transport - one
server process, one lifespan, one supervisor - exercises the exact path the
systemd deployment relies on.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from fastmcp import Client
from mcp import types as mcp_types
from mcp.shared.exceptions import McpError

from mcp_call_orchestrator_proxy.app import create_proxy_app
from mcp_call_orchestrator_proxy.config import ProxySettings

pytestmark = pytest.mark.component


class GatedEchoBackend:
    """An echo backend whose connection is gated by a shared ``gate`` flag."""

    def __init__(self, gate: dict[str, bool]) -> None:
        self._gate = gate
        self._entered = False

    async def __aenter__(self) -> GatedEchoBackend:
        if not self._gate["up"]:
            raise RuntimeError("Client failed to connect: simulated Backend down")
        self._entered = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._entered = False

    def is_connected(self) -> bool:
        return self._entered

    async def initialize(self) -> mcp_types.InitializeResult:
        return mcp_types.InitializeResult(
            protocolVersion=mcp_types.LATEST_PROTOCOL_VERSION,
            capabilities=mcp_types.ServerCapabilities(),
            serverInfo=mcp_types.Implementation(name="gated-echo", version="0"),
        )

    async def ping(self) -> bool:
        return True

    async def list_tools(self) -> list[mcp_types.Tool]:
        return [
            mcp_types.Tool(
                name="echo",
                description="Echo the given value back verbatim.",
                inputSchema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            )
        ]

    async def call_tool_mcp(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        meta: dict[str, Any] | None = None,
    ) -> mcp_types.CallToolResult:
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=str(arguments["value"]))]
        )

    async def list_resources(self) -> list[mcp_types.Resource]:
        return []

    async def list_resource_templates(self) -> list[mcp_types.ResourceTemplate]:
        return []

    async def read_resource(
        self, uri: str
    ) -> list[mcp_types.TextResourceContents | mcp_types.BlobResourceContents]:
        return []

    async def list_prompts(self) -> list[mcp_types.Prompt]:
        return []

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> mcp_types.GetPromptResult:
        return mcp_types.GetPromptResult(messages=[])


class SessionTerminatingBackend(GatedEchoBackend):
    """A backend whose streamable-HTTP session can be cleanly terminated.

    Connects and serves ``echo`` normally (an always-up ``GatedEchoBackend``). Once
    ``terminate()`` is called, every delegated call raises the exact error the MCP
    streamable-HTTP client raises when a backend answers a request on a now-unknown
    session id - ``McpError(code=32600, "Session terminated")`` - the clean-404 path
    a backend restart takes when its HTTP listener never drops. ``is_connected()``
    keeps returning True while terminated: the listener is up, only the session is
    gone, so the break can be learned only from a failing call, not the transport. A
    fresh instance (what the supervisor builds on reconnect) is a fresh session and
    serves normally again.
    """

    def __init__(self) -> None:
        super().__init__(gate={"up": True})
        self._terminated = False

    def terminate(self) -> None:
        self._terminated = True

    def _guard(self) -> None:
        if self._terminated:
            raise McpError(
                mcp_types.ErrorData(code=32600, message="Session terminated")
            )

    async def ping(self) -> bool:
        self._guard()
        return await super().ping()

    async def list_tools(self) -> list[mcp_types.Tool]:
        self._guard()
        return await super().list_tools()

    async def call_tool_mcp(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        meta: dict[str, Any] | None = None,
    ) -> mcp_types.CallToolResult:
        self._guard()
        return await super().call_tool_mcp(name, arguments, meta=meta)


def _status_payload(result: Any) -> dict[str, Any]:
    """Pull the backend_status dict out of a CallToolResult, tolerant of shape."""
    if isinstance(result.data, dict):
        return result.data
    structured = result.structured_content or {}
    inner = structured.get("result")
    return inner if isinstance(inner, dict) else structured


@pytest.mark.asyncio
async def test_proxy_survives_backend_absence_then_reconnects(serve_over_http) -> None:
    """Start with Backend down; the proxy still serves, then self-heals when it
    comes up - no restart, no hang."""
    settings = ProxySettings(
        backend_api_key="a" * 32,
        connect_timeout_seconds=2.0,
        reconnect_initial_backoff_seconds=0.05,
        reconnect_max_backoff_seconds=0.2,
        backend_health_poll_seconds=0.05,
    )
    gate = {"up": False}

    def factory() -> GatedEchoBackend:
        return GatedEchoBackend(gate)

    proxy_app = create_proxy_app(settings=settings, backend_factory=factory)

    async with serve_over_http(proxy_app.mcp) as url, Client(url) as client:
        # (1) The proxy is connectable while Backend is down: an agent can ask
        #     backend_status without triggering a failing backend call.
        status = await client.call_tool("backend_status", {})
        assert _status_payload(status)["connected"] is False

        # (1b) Backend tools degrade out of the listing (provider_error "warn"),
        #      leaving only the proxy-native status tool.
        down_tools = {t.name for t in await client.list_tools()}
        assert down_tools == {"backend_status"}, down_tools

        # (2) Backend starts. The supervisor reconnects on its own.
        gate["up"] = True

        async def _connected() -> bool:
            snap = await client.call_tool("backend_status", {})
            return bool(_status_payload(snap)["connected"])

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if await _connected():
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("proxy never reconnected after the backend came up")

        # (3) Full tool parity is restored and a real call round-trips.
        up_tools = {t.name for t in await client.list_tools()}
        assert up_tools == {"backend_status", "echo"}, up_tools

        echoed = await client.call_tool("echo", {"value": "hello"})
        assert echoed.content[0].text == "hello"


@pytest.mark.asyncio
async def test_proxy_self_heals_after_a_terminated_backend_session(
    serve_over_http,
) -> None:
    """A backend that restarts with its HTTP listener still up rejects the proxy's
    stale session with a clean 404 - surfaced as ``McpError`` "Session terminated",
    not a dropped connection. The proxy must recognise that as a break and reconnect
    on its own, with no restart. This is the exact failure that once left the proxy
    serving zero tools until its service was restarted by hand."""
    settings = ProxySettings(
        backend_api_key="a" * 32,
        connect_timeout_seconds=2.0,
        reconnect_initial_backoff_seconds=0.05,
        reconnect_max_backoff_seconds=0.2,
        backend_health_poll_seconds=0.05,
    )
    built: list[SessionTerminatingBackend] = []

    def factory() -> SessionTerminatingBackend:
        backend = SessionTerminatingBackend()
        built.append(backend)
        return backend

    proxy_app = create_proxy_app(settings=settings, backend_factory=factory)

    async with serve_over_http(proxy_app.mcp) as url, Client(url) as client:
        # (1) Connected and serving: full parity, a real call round-trips.
        assert {t.name for t in await client.list_tools()} == {"backend_status", "echo"}
        before = await client.call_tool("echo", {"value": "before"})
        assert before.content[0].text == "before"

        # (2) The backend forgets the live session (listener still up). The next
        #     backend-touching call carries the now-dead session and 404s; the
        #     tool listing degrades to just backend_status while that trips the
        #     break, without the proxy being restarted.
        assert built, "the supervisor never built a backend connection"
        built[-1].terminate()
        # The incident symptom: the terminated session makes the backend's tools
        # vanish from the listing (degraded to just backend_status) - and that
        # very query is what trips the break for the supervisor to act on.
        assert {t.name for t in await client.list_tools()} == {"backend_status"}

        # (3) The supervisor reconnects on its own.
        async def _connected() -> bool:
            snap = await client.call_tool("backend_status", {})
            return bool(_status_payload(snap)["connected"])

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if await _connected():
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("proxy never reconnected after the session was terminated")

        # (4) Parity is restored and a real call round-trips again - no restart.
        assert {t.name for t in await client.list_tools()} == {"backend_status", "echo"}
        after = await client.call_tool("echo", {"value": "after"})
        assert after.content[0].text == "after"
