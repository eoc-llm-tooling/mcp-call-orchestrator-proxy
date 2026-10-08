"""Unit tests for the self-healing backend connection.

Drive ``ResilientBackend`` with a controllable fake backend whose behaviour the
test flips over time (connect fails/succeeds, a live call drops the connection).
Backoff values are tiny so the supervisor's retry loop runs fast.

Verifies the resilience contract:
- starts cleanly when the backend is absent (``__aenter__`` never raises);
- a call while down returns a clean ``BackendUnavailableError``;
- ``initialize()`` while down returns a synthetic result (never raises), so an
  agent can still connect to the proxy;
- connects on its own when the backend appears;
- reconnects after a mid-session drop;
- shuts down *promptly* even mid-backoff, and cleanly while connected.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from mcp import types as mcp_types
from mcp.shared.exceptions import McpError

from mcp_call_orchestrator_proxy.interfaces import MCPBackendClient
from mcp_call_orchestrator_proxy.resilient import (
    BackendUnavailableError,
    ResilientBackend,
)


class BackendController:
    """Hands out ``FakeClient`` instances; the test flips its flags over time."""

    def __init__(self) -> None:
        self.connect_ok = True
        self.tool_error: Exception | None = None
        self.built: list[FakeClient] = []

    def __call__(self) -> MCPBackendClient:
        client = FakeClient(self)
        self.built.append(client)
        return client


class FakeClient:
    """A controllable backend client honoring the ``MCPBackendClient`` protocol."""

    def __init__(self, controller: BackendController) -> None:
        self._controller = controller
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> FakeClient:
        if not self._controller.connect_ok:
            raise RuntimeError("Client failed to connect: simulated backend down")
        self.entered = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.exited = True

    def is_connected(self) -> bool:
        return self.entered and not self.exited

    async def initialize(self) -> mcp_types.InitializeResult:
        return mcp_types.InitializeResult(
            protocolVersion="2025-06-18",
            capabilities=mcp_types.ServerCapabilities(),
            serverInfo=mcp_types.Implementation(name="fake", version="0"),
        )

    async def ping(self) -> bool:
        return True

    async def list_tools(self) -> list[mcp_types.Tool]:
        return [
            mcp_types.Tool(
                name="echo",
                description="echo",
                inputSchema={"type": "object", "properties": {}},
            )
        ]

    async def call_tool_mcp(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        meta: dict[str, Any] | None = None,
    ) -> mcp_types.CallToolResult:
        if self._controller.tool_error is not None:
            raise self._controller.tool_error
        return mcp_types.CallToolResult(
            content=[
                mcp_types.TextContent(type="text", text=str(arguments.get("value", "")))
            ]
        )

    async def list_resources(self) -> list[mcp_types.Resource]:
        return []

    async def list_resource_templates(self) -> list[mcp_types.ResourceTemplate]:
        return []

    async def read_resource(self, uri: str) -> list[Any]:
        return []

    async def list_prompts(self) -> list[mcp_types.Prompt]:
        return []

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> mcp_types.GetPromptResult:
        return mcp_types.GetPromptResult(messages=[])


def _make_backend(
    controller: BackendController, **overrides: float
) -> ResilientBackend:
    params: dict[str, float] = {
        "connect_timeout": 1.0,
        "initial_backoff": 0.01,
        "max_backoff": 0.05,
        "backoff_multiplier": 2.0,
        "health_poll": 0.01,
    }
    params.update(overrides)
    return ResilientBackend(controller, backend_url="http://test/mcp/", **params)


async def _wait_until(predicate, timeout: float = 3.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def _await_until(coro_predicate, timeout: float = 3.0, interval: float = 0.01):
    """Like ``_wait_until`` but for an async predicate that is awaited each poll."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await coro_predicate():
            return True
        await asyncio.sleep(interval)
    return False


@pytest.mark.asyncio
async def test_starts_cleanly_when_backend_absent() -> None:
    """A down backend must not crash startup; calls degrade to clean errors."""
    controller = BackendController()
    controller.connect_ok = False

    async with _make_backend(controller) as backend:
        assert backend.status_snapshot()["connected"] is False

        # initialize() must NOT raise - the proxy stays connectable while down.
        result = await backend.initialize()
        assert isinstance(result, mcp_types.InitializeResult)

        # Operations that need the backend fail cleanly, not with a hang.
        with pytest.raises(BackendUnavailableError):
            await backend.call_tool_mcp("echo", {"value": "x"})
        with pytest.raises(BackendUnavailableError):
            await backend.list_tools()

        assert await backend.ping() is False


@pytest.mark.asyncio
async def test_connects_when_backend_appears() -> None:
    """The supervisor must connect on its own once the backend comes up."""
    controller = BackendController()
    controller.connect_ok = False

    async with _make_backend(controller) as backend:
        assert backend.status_snapshot()["connected"] is False

        controller.connect_ok = True  # backend mcp starts

        assert await _wait_until(
            lambda: backend.status_snapshot()["connected"] is True
        ), "backend never reconnected after it became reachable"

        result = await backend.call_tool_mcp("echo", {"value": "hi"})
        assert result.content[0].text == "hi"


@pytest.mark.asyncio
async def test_reconnects_after_mid_session_drop() -> None:
    """A live call that fails with a connection error triggers a reconnect."""
    controller = BackendController()

    async with _make_backend(controller) as backend:
        assert await _wait_until(lambda: backend.status_snapshot()["connected"] is True)
        first = await backend.call_tool_mcp("echo", {"value": "a"})
        assert first.content[0].text == "a"
        clients_before = len(controller.built)

        # Backend drops the session: calls fail with a connection error, which
        # must force the supervisor to tear down and rebuild the connection.
        controller.tool_error = RuntimeError("Server session was closed unexpectedly")
        with pytest.raises(BackendUnavailableError):
            await backend.call_tool_mcp("echo", {"value": "b"})

        assert await _wait_until(lambda: len(controller.built) > clients_before), (
            "supervisor did not re-establish a connection after the drop"
        )

        # Backend fully recovers; a call must succeed again on the fresh session.
        controller.tool_error = None

        async def _call_succeeds() -> bool:
            try:
                await backend.call_tool_mcp("echo", {"value": "c"})
            except BackendUnavailableError:
                return False
            return True

        assert await _await_until(_call_succeeds), "backend never recovered after drop"


@pytest.mark.asyncio
async def test_reconnects_after_a_terminated_session() -> None:
    """A backend that keeps its listener up but rejects the stale session answers
    with a clean 404, which the SDK surfaces as ``McpError(32600, "Session
    terminated")``. That is a broken connection, not a transport error, and it must
    still force the supervisor to rebuild - the defect that once left the proxy
    serving zero tools until a manual restart."""
    controller = BackendController()

    async with _make_backend(controller) as backend:
        assert await _wait_until(lambda: backend.status_snapshot()["connected"] is True)
        first = await backend.call_tool_mcp("echo", {"value": "a"})
        assert first.content[0].text == "a"
        clients_before = len(controller.built)

        controller.tool_error = McpError(
            mcp_types.ErrorData(code=32600, message="Session terminated")
        )
        with pytest.raises(BackendUnavailableError):
            await backend.call_tool_mcp("echo", {"value": "b"})

        assert await _wait_until(lambda: len(controller.built) > clients_before), (
            "supervisor did not rebuild the connection after the session was terminated"
        )

        controller.tool_error = None

        async def _call_succeeds() -> bool:
            try:
                await backend.call_tool_mcp("echo", {"value": "c"})
            except BackendUnavailableError:
                return False
            return True

        assert await _await_until(_call_succeeds), "backend never recovered"


@pytest.mark.asyncio
async def test_reconnects_after_the_sdks_generic_connection_closed_signal() -> None:
    """A dead subprocess (a stdio backend's own failure mode) never reaches us as a
    raw transport error: the SDK's receive loop swallows the pipe closing and
    drains every pending request with ``McpError(-32000, "Connection closed")``
    instead. That generic signal must force a rebuild too, not just the
    streamable-HTTP-specific "session terminated" one."""
    controller = BackendController()

    async with _make_backend(controller) as backend:
        assert await _wait_until(lambda: backend.status_snapshot()["connected"] is True)
        first = await backend.call_tool_mcp("echo", {"value": "a"})
        assert first.content[0].text == "a"
        clients_before = len(controller.built)

        controller.tool_error = McpError(
            mcp_types.ErrorData(code=-32000, message="Connection closed")
        )
        with pytest.raises(BackendUnavailableError):
            await backend.call_tool_mcp("echo", {"value": "b"})

        assert await _wait_until(lambda: len(controller.built) > clients_before), (
            "supervisor did not rebuild the connection after a connection-closed signal"
        )

        controller.tool_error = None

        async def _call_succeeds() -> bool:
            try:
                await backend.call_tool_mcp("echo", {"value": "c"})
            except BackendUnavailableError:
                return False
            return True

        assert await _await_until(_call_succeeds), "backend never recovered"


@pytest.mark.asyncio
async def test_ordinary_mcp_error_is_not_a_connection_break() -> None:
    """An unknown or failed tool call also raises ``McpError``. Only the terminated-
    session signal is a break; any other ``McpError`` is a per-call error that must
    propagate unchanged and leave the connection live - otherwise a single bad call
    would tear down the whole backend connection."""
    controller = BackendController()

    async with _make_backend(controller) as backend:
        assert await _wait_until(lambda: backend.status_snapshot()["connected"] is True)
        clients_before = len(controller.built)

        controller.tool_error = McpError(
            mcp_types.ErrorData(code=-32601, message="Method not found")
        )
        with pytest.raises(McpError):
            await backend.call_tool_mcp("echo", {"value": "b"})

        # The connection is untouched: no rebuild, still reporting connected.
        assert len(controller.built) == clients_before, (
            "an ordinary McpError wrongly tore down and rebuilt the connection"
        )
        assert backend.status_snapshot()["connected"] is True


@pytest.mark.asyncio
async def test_shutdown_is_prompt_during_backoff() -> None:
    """SIGTERM mid-backoff must tear down immediately, not wait out the backoff."""
    controller = BackendController()
    controller.connect_ok = False

    # A huge backoff: if the sleep were not interruptible, __aexit__ would hang.
    backend = _make_backend(controller, initial_backoff=100.0, max_backoff=100.0)
    await backend.__aenter__()

    started = time.monotonic()
    await backend.__aexit__(None, None, None)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"shutdown stalled on the backoff sleep ({elapsed:.2f}s)"


@pytest.mark.asyncio
async def test_clean_shutdown_while_connected() -> None:
    """Shutting down while connected closes the live client and leaks no task."""
    controller = BackendController()

    backend = _make_backend(controller)
    await backend.__aenter__()
    assert await _wait_until(lambda: backend.status_snapshot()["connected"] is True)
    live = controller.built[-1]

    await backend.__aexit__(None, None, None)

    assert live.exited is True, "live backend connection was not closed on shutdown"
    assert backend._supervisor_task is None, "supervisor task was not torn down"


@pytest.mark.asyncio
async def test_backoff_is_bounded_while_down() -> None:
    """While down, retries keep happening and the backoff never exceeds the cap."""
    controller = BackendController()
    controller.connect_ok = False

    async with _make_backend(controller) as backend:
        assert await _wait_until(
            lambda: backend.status_snapshot()["consecutive_failures"] >= 3
        ), "supervisor did not keep retrying a down backend"
        assert backend.status_snapshot()["current_backoff_seconds"] <= 0.05
