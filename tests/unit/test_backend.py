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

from mcp_call_orchestrator_proxy import backend as backend_module
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

    @property
    def protocol_version(self) -> str | None:
        return "2026-07-28"

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

    async def read_resource_mcp(
        self, uri: str, meta: dict[str, Any] | None = None
    ) -> mcp_types.ReadResourceResult:
        self.calls.append(f"read_resource_mcp:{uri}:{meta}")
        return await self._simulate(mcp_types.ReadResourceResult(contents=[]))

    async def list_prompts(self) -> list[mcp_types.Prompt]:
        self.calls.append("list_prompts")
        return await self._simulate([])

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        meta: dict[str, Any] | None = None,
    ) -> mcp_types.GetPromptResult:
        self.calls.append(f"get_prompt:{name}:{meta}")
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
        await backend.list_tools()
        await backend.list_resources()
        await backend.list_resource_templates()
        await backend.read_resource_mcp("res://x", meta={"k": "v"})
        await backend.list_prompts()
        await backend.get_prompt("greet", {"name": "a"}, meta={"k": "v"})

    assert upstream.calls == [
        "list_tools",
        "list_resources",
        "list_resource_templates",
        "read_resource_mcp:res://x:{'k': 'v'}",
        "list_prompts",
        "get_prompt:greet:{'k': 'v'}",
    ]


def test_facade_reports_no_protocol_version_whatever_the_backend_negotiated() -> None:
    """FastMCP's provider calls the backend's session directly for a 2026-07-28+
    version, which would bypass the queue; the facade must never report one."""
    backend = QueuedMCPBackend(FakeUpstream(), CallOrchestrator())
    assert backend.protocol_version is None


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


HANDSHAKE_VERSION = "2025-11-25"
MODERN_VERSION = "2026-07-28"


def client_params(name: str) -> mcp_types.InitializeRequestParams:
    """What the SDK records for a client, from ``initialize`` or a request envelope."""
    return mcp_types.InitializeRequestParams(
        protocol_version=HANDSHAKE_VERSION,
        capabilities=mcp_types.ClientCapabilities(),
        client_info=mcp_types.Implementation(name=name, version="1"),
    )


@contextmanager
def serving(
    monkeypatch: pytest.MonkeyPatch,
    params: mcp_types.InitializeRequestParams | None,
    protocol_version: str = HANDSHAKE_VERSION,
) -> Iterator[None]:
    """Pretend we are inside an MCP request from a client with ``params``.

    The SDK builds a fresh session object for every request; only the client's
    params object is shared by the requests of one handshake-era session.
    """
    request = SimpleNamespace(
        session=SimpleNamespace(client_params=params),
        protocol_version=protocol_version,
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            backend_module,
            "get_context",
            lambda: SimpleNamespace(request_context=request),
        )
        yield


def test_a_client_keeps_one_name_for_the_life_of_its_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attribution is worthless if a client's name drifts between its own calls."""
    backend = QueuedMCPBackend(FakeUpstream(), CallOrchestrator())
    params = client_params("agent")

    with serving(monkeypatch, params):
        first = backend._client_label()
    with serving(monkeypatch, params):
        second = backend._client_label()

    assert first == second == "agent-001"


def test_two_sessions_of_one_client_get_different_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = QueuedMCPBackend(FakeUpstream(), CallOrchestrator())
    first, second = client_params("agent"), client_params("agent")

    with serving(monkeypatch, first):
        first_tag = backend._client_label()
    with serving(monkeypatch, second):
        second_tag = backend._client_label()

    assert (first_tag, second_tag) == ("agent-001", "agent-002")


def test_a_disconnected_client_never_lends_its_name_to_the_next_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tag must stay unique for the process lifetime, not just while the
    session is alive.

    This is the trap an id()-derived tag falls into: CPython reuses the memory
    address of a collected object, so on an always-on proxy - where clients
    connect and disconnect all day - two unrelated agents end up logging under
    the same name, and a day-old log attributes one client's calls to another.
    Here each session is dropped and collected before the next is made, which is
    precisely the condition that produced the collision.
    """
    backend = QueuedMCPBackend(FakeUpstream(), CallOrchestrator())
    seen: list[str] = []

    for _ in range(50):
        params = client_params("agent")
        with serving(monkeypatch, params):
            seen.append(backend._client_label())
        del params
        gc.collect()  # the client has gone; its session is now collectable

    assert len(set(seen)) == 50, (
        "sequential clients shared a log identity: "
        f"{len(seen) - len(set(seen))} collision(s) in {len(seen)} sessions"
    )
    assert backend._session_tags == {}, "a collected session's tag was kept"


def test_a_sessionless_client_is_named_without_a_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """From 2026-07-28 there is no session: the tag is the name the request
    carries, and two instances of one client share it."""
    backend = QueuedMCPBackend(FakeUpstream(), CallOrchestrator())

    with serving(monkeypatch, client_params("agent"), MODERN_VERSION):
        first = backend._client_label()
    with serving(monkeypatch, client_params("agent"), MODERN_VERSION):
        second = backend._client_label()

    assert first == second == "agent"


@pytest.mark.parametrize("protocol_version", [HANDSHAKE_VERSION, MODERN_VERSION])
def test_a_client_that_gives_no_name_is_called_client(
    monkeypatch: pytest.MonkeyPatch, protocol_version: str
) -> None:
    backend = QueuedMCPBackend(FakeUpstream(), CallOrchestrator())

    with serving(monkeypatch, None, protocol_version):
        assert backend._client_label() == "client"
    with serving(monkeypatch, client_params(""), protocol_version):
        assert backend._client_label().startswith("client")


def test_a_call_with_no_client_request_behind_it_is_the_proxys() -> None:
    """A call the proxy makes for itself has no request context. That is normal,
    not a fault, so it is named rather than raising.
    """
    backend = QueuedMCPBackend(FakeUpstream(), CallOrchestrator())
    assert backend._client_label() == "proxy"
