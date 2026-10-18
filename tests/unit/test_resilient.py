"""Unit tests for the self-healing backend connection.

Drive ``ResilientBackend`` with a controllable fake backend whose behaviour the
test flips over time (connect fails/succeeds, a live call drops the connection).
Backoff values are tiny so the supervisor's retry loop runs fast.

Verifies the resilience contract:
- starts cleanly when the backend is absent (``__aenter__`` never raises);
- a call while down returns a clean ``BackendUnavailableError``;
- what the backend negotiated is recorded on connect and kept while it is down,
  so the proxy can answer its own clients' handshakes from it;
- connects on its own when the backend appears;
- reconnects after a mid-session drop;
- shuts down *promptly* even mid-backoff, and cleanly while connected.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from mcp import MCPError
from mcp import types as mcp_types

from mcp_call_orchestrator_proxy.interfaces import NegotiatedBackendClient
from mcp_call_orchestrator_proxy.resilient import (
    BackendMetadata,
    BackendUnavailableError,
    MethodNotSupportedError,
    ResilientBackend,
)


class BackendController:
    """Hands out ``FakeClient`` instances; the test flips its flags over time."""

    def __init__(self) -> None:
        self.connect_ok = True
        self.tool_error: Exception | None = None
        self.instructions: str | None = "BACKEND INSTRUCTIONS"
        self.built: list[FakeClient] = []

    def __call__(self) -> NegotiatedBackendClient:
        client = FakeClient(self)
        self.built.append(client)
        return client


class FakeClient:
    """A controllable backend client honoring ``NegotiatedBackendClient``."""

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

    @property
    def protocol_version(self) -> str | None:
        return "2026-07-28" if self.is_connected() else None

    @property
    def server_capabilities(self) -> mcp_types.ServerCapabilities | None:
        return mcp_types.ServerCapabilities(tools=mcp_types.ToolsCapability())

    @property
    def instructions(self) -> str | None:
        return self._controller.instructions

    async def list_tools(self) -> list[mcp_types.Tool]:
        return [
            mcp_types.Tool(
                name="echo",
                description="echo",
                input_schema={"type": "object", "properties": {}},
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

    async def read_resource_mcp(
        self, uri: str, meta: dict[str, Any] | None = None
    ) -> mcp_types.ReadResourceResult:
        return mcp_types.ReadResourceResult(contents=[])

    async def list_prompts(self) -> list[mcp_types.Prompt]:
        return []

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        meta: dict[str, Any] | None = None,
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
        assert backend.metadata is None

        # Operations that need the backend fail cleanly, not with a hang.
        with pytest.raises(BackendUnavailableError):
            await backend.call_tool_mcp("echo", {"value": "x"})
        with pytest.raises(BackendUnavailableError):
            await backend.list_tools()


@pytest.mark.asyncio
async def test_records_what_the_backend_negotiated_and_keeps_it_while_down() -> None:
    controller = BackendController()

    async with _make_backend(controller) as backend:
        assert await _wait_until(lambda: backend.status_snapshot()["connected"] is True)
        connected = BackendMetadata(
            protocol_version="2026-07-28",
            capabilities=mcp_types.ServerCapabilities(
                tools=mcp_types.ToolsCapability()
            ),
            instructions="BACKEND INSTRUCTIONS",
        )
        assert backend.metadata == connected

        controller.connect_ok = False
        controller.tool_error = RuntimeError("Server session was closed unexpectedly")
        with pytest.raises(BackendUnavailableError):
            await backend.call_tool_mcp("echo", {"value": "x"})
        assert await _wait_until(
            lambda: backend.status_snapshot()["consecutive_failures"] >= 1
        )
        assert backend.metadata == connected


def test_reports_no_protocol_version_to_the_layers_above() -> None:
    """See ``MCPBackendClient.protocol_version``: a modern version here would make
    FastMCP's provider bypass the queue."""
    assert _make_backend(BackendController()).protocol_version is None


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
@pytest.mark.parametrize(
    ("code", "message"),
    [
        (mcp_types.INVALID_REQUEST, "Session terminated"),
        (mcp_types.INVALID_REQUEST, "Session not found"),
        (-32001, "Session not found"),
    ],
    ids=["sdk-stand-in", "python-sdk-server", "typescript-sdk-server"],
)
async def test_reconnects_after_a_terminated_session(code: int, message: str) -> None:
    """A backend that keeps its listener up but rejects the stale session answers
    with a clean 404. The SDK surfaces the JSON-RPC error the backend put in the
    404's body (each MCP SDK's server words it its own way), or its own stand-in
    when there is none. That is a broken connection, not a transport error, and
    it must still force the supervisor to rebuild - the defect that once left the
    proxy serving zero tools until a manual restart."""
    controller = BackendController()

    async with _make_backend(controller) as backend:
        assert await _wait_until(lambda: backend.status_snapshot()["connected"] is True)
        first = await backend.call_tool_mcp("echo", {"value": "a"})
        assert first.content[0].text == "a"
        clients_before = len(controller.built)

        controller.tool_error = MCPError(code=code, message=message)
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
    drains every pending request with ``MCPError(-32000, "Connection closed")``
    instead. That generic signal must force a rebuild too, not just the
    streamable-HTTP-specific "session terminated" one."""
    controller = BackendController()

    async with _make_backend(controller) as backend:
        assert await _wait_until(lambda: backend.status_snapshot()["connected"] is True)
        first = await backend.call_tool_mcp("echo", {"value": "a"})
        assert first.content[0].text == "a"
        clients_before = len(controller.built)

        controller.tool_error = MCPError(
            code=mcp_types.CONNECTION_CLOSED, message="Connection closed"
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
@pytest.mark.parametrize(
    ("code", "message"),
    [
        (-32602, "Invalid params"),
        # The code a TypeScript SDK server gives a lost session, on another error.
        (-32001, "Request 'tools/call' timed out"),
    ],
    ids=["invalid-params", "request-timeout"],
)
async def test_ordinary_mcp_error_is_not_a_connection_break(
    code: int, message: str
) -> None:
    """An unknown or failed tool call also raises ``MCPError``. Only the terminated-
    session signal is a break; any other ``MCPError`` is a per-call error that must
    propagate unchanged and leave the connection live - otherwise a single bad call
    would tear down the whole backend connection."""
    controller = BackendController()

    async with _make_backend(controller) as backend:
        assert await _wait_until(lambda: backend.status_snapshot()["connected"] is True)
        clients_before = len(controller.built)

        controller.tool_error = MCPError(code=code, message=message)
        with pytest.raises(MCPError) as raised:
            await backend.call_tool_mcp("echo", {"value": "b"})
        assert raised.value is controller.tool_error

        # The connection is untouched: no rebuild, still reporting connected.
        assert len(controller.built) == clients_before, (
            "an ordinary MCPError wrongly tore down and rebuilt the connection"
        )
        assert backend.status_snapshot()["connected"] is True


@pytest.mark.asyncio
async def test_method_not_found_is_raised_as_method_not_supported() -> None:
    """A backend's "Method not found" answer gets its own type, keeps its
    ``ErrorData`` (FastMCP reads the code), and leaves the connection live."""
    controller = BackendController()

    async with _make_backend(controller) as backend:
        assert await _wait_until(lambda: backend.status_snapshot()["connected"] is True)
        clients_before = len(controller.built)

        error = mcp_types.ErrorData(code=-32601, message="Method not found")
        controller.tool_error = MCPError.from_error_data(error)
        with pytest.raises(MethodNotSupportedError) as raised:
            await backend.call_tool_mcp("echo", {"value": "b"})
        assert raised.value.error == error

        assert len(controller.built) == clients_before
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
