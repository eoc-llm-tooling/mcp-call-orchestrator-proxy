"""Unit tests for the queue-aware backend facade.

Verifies delegation correctness, that calls are actually serialized
through the orchestrator, and that the facade's own __aenter__/__aexit__
are true no-ops - the least obvious, most regression-prone part of this
design, since FastMCP wraps every single operation in `async with client:`.
"""

from __future__ import annotations

import asyncio
import gc
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, TypeVar

import pytest
from mcp import types as mcp_types
from mcp.server.lowlevel.server import request_ctx

from mcp_call_orchestrator_proxy.backend import QueuedMCPBackend
from mcp_call_orchestrator_proxy.orchestrator import CallOrchestrator

T = TypeVar("T")


class FakeUpstream:
    """Records calls and simulates a backend that cannot handle overlap."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.enter_count = 0
        self.exit_count = 0
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0

    async def __aenter__(self) -> FakeUpstream:
        self.enter_count += 1
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.exit_count += 1

    async def _simulate(self, result: T) -> T:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(self.delay)
        self.active -= 1
        return result

    async def initialize(self) -> mcp_types.InitializeResult:
        self.calls.append("initialize")
        return await self._simulate(
            mcp_types.InitializeResult(
                protocolVersion="2025-06-18",
                capabilities=mcp_types.ServerCapabilities(),
                serverInfo=mcp_types.Implementation(name="fake", version="0"),
            )
        )

    async def ping(self) -> bool:
        self.calls.append("ping")
        return await self._simulate(True)

    async def list_tools(self) -> list[mcp_types.Tool]:
        self.calls.append("list_tools")
        return await self._simulate([])

    async def call_tool_mcp(
        self,
        name: str,
        arguments: dict[str, Any],
        meta: dict[str, Any] | None = None,
    ) -> mcp_types.CallToolResult:
        self.calls.append(f"call_tool_mcp:{name}:{arguments}")
        return await self._simulate(
            mcp_types.CallToolResult(
                content=[mcp_types.TextContent(type="text", text="ok")]
            )
        )

    async def list_resources(self) -> list[mcp_types.Resource]:
        self.calls.append("list_resources")
        return await self._simulate([])

    async def list_resource_templates(self) -> list[mcp_types.ResourceTemplate]:
        self.calls.append("list_resource_templates")
        return await self._simulate([])

    async def read_resource(self, uri: str) -> list[Any]:
        self.calls.append(f"read_resource:{uri}")
        return await self._simulate([])

    async def list_prompts(self) -> list[mcp_types.Prompt]:
        self.calls.append("list_prompts")
        return await self._simulate([])

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> mcp_types.GetPromptResult:
        self.calls.append(f"get_prompt:{name}")
        return await self._simulate(mcp_types.GetPromptResult(messages=[]))


@pytest.mark.asyncio
async def test_facade_delegates_call_tool_mcp_with_exact_arguments() -> None:
    upstream = FakeUpstream()
    async with CallOrchestrator() as orch:
        backend = QueuedMCPBackend(upstream, orch)
        result = await backend.call_tool_mcp("vault_read", {"path": "x.md"})

    assert isinstance(result.content[0], mcp_types.TextContent)
    assert result.content[0].text == "ok"
    assert upstream.calls == ["call_tool_mcp:vault_read:{'path': 'x.md'}"]


@pytest.mark.asyncio
async def test_facade_delegates_all_backend_methods() -> None:
    upstream = FakeUpstream()
    async with CallOrchestrator() as orch:
        backend = QueuedMCPBackend(upstream, orch)
        await backend.initialize()
        await backend.ping()
        await backend.list_tools()
        await backend.list_resources()
        await backend.list_resource_templates()
        await backend.read_resource("res://x")
        await backend.list_prompts()
        await backend.get_prompt("greet", {"name": "a"})

    assert upstream.calls == [
        "initialize",
        "ping",
        "list_tools",
        "list_resources",
        "list_resource_templates",
        "read_resource:res://x",
        "list_prompts",
        "get_prompt:greet",
    ]


@pytest.mark.asyncio
async def test_facade_serializes_calls_through_the_orchestrator() -> None:
    """Concurrent calls through the facade must still hit the backend one at a time."""
    upstream = FakeUpstream(delay=0.05)
    async with CallOrchestrator(max_concurrent=1) as orch:
        backend = QueuedMCPBackend(upstream, orch)
        await asyncio.gather(*(backend.list_tools() for _ in range(4)))

    assert upstream.max_active == 1


@pytest.mark.asyncio
async def test_facade_aenter_aexit_are_no_ops() -> None:
    """The facade must never open/close the real connection per-call.

    FastMCP's proxy layer wraps every backend touchpoint in
    `async with client:` - the real connection is opened once, for the
    whole app lifetime, elsewhere (the composition root's lifespan hook).
    """
    upstream = FakeUpstream()
    async with CallOrchestrator() as orch:
        backend = QueuedMCPBackend(upstream, orch)
        async with backend:
            pass
        async with backend:
            await backend.list_tools()

    assert upstream.enter_count == 0
    assert upstream.exit_count == 0


class FakeSession:
    """Stands in for MCP's ServerSession - only what the label logic reads."""

    def __init__(self, client_name: str) -> None:
        self.client_params = SimpleNamespace(
            clientInfo=SimpleNamespace(name=client_name)
        )


@contextmanager
def serving(session: FakeSession) -> Iterator[None]:
    """Pretend we are inside an MCP request from `session`."""
    token = request_ctx.set(SimpleNamespace(session=session))  # type: ignore[arg-type]
    try:
        yield
    finally:
        request_ctx.reset(token)


def test_a_client_keeps_one_name_for_the_life_of_its_session() -> None:
    """Attribution is worthless if a client's name drifts between its own calls."""
    backend = QueuedMCPBackend(FakeUpstream(), CallOrchestrator())
    session = FakeSession("agent")

    with serving(session):
        first = backend._client_label()
        second = backend._client_label()

    assert first == second
    assert "agent" in first


def test_a_disconnected_client_never_lends_its_name_to_the_next_one() -> None:
    """The tag must stay unique for the process lifetime, not just while the
    session is alive.

    This is the trap an id()-derived tag falls into: CPython reuses the memory
    address of a collected session, so on an always-on proxy - where clients
    connect and disconnect all day - two unrelated agents end up logging under
    the same name, and a day-old log attributes one client's calls to another.
    Here each session is dropped and collected before the next is made, which is
    precisely the condition that produced the collision.
    """
    backend = QueuedMCPBackend(FakeUpstream(), CallOrchestrator())
    seen: list[str] = []

    for _ in range(50):
        session = FakeSession("agent")
        with serving(session):
            seen.append(backend._client_label())
        del session
        gc.collect()  # the client has gone; its session is now collectable

    assert len(set(seen)) == 50, (
        "sequential clients shared a log identity: "
        f"{len(seen) - len(set(seen))} collision(s) in {len(seen)} sessions"
    )


def test_a_call_with_no_session_behind_it_is_still_named() -> None:
    """Discovery the proxy performs for itself has no request context. That is
    normal, not a fault, so it must degrade to a placeholder rather than raise.
    """
    backend = QueuedMCPBackend(FakeUpstream(), CallOrchestrator())
    assert backend._client_label() == "no-session"
