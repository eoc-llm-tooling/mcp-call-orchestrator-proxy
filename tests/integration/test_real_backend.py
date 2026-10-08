"""Integration tests against a real (user-supplied) MCP backend.

These tests exercise the proxy's core guarantees — exact schema parity,
call serialization under load, multi-session isolation over real HTTP,
and basic resilience — against whatever backend is configured via
tests/integration/backend.json.

No backend-specific tool names or assumptions are hardcoded.

Run with:
    pytest -m integration
"""

import asyncio

import pytest
from fastmcp import Client

from mcp_call_orchestrator_proxy.app import create_proxy_app
from mcp_call_orchestrator_proxy.backend import QueuedMCPBackend
from mcp_call_orchestrator_proxy.client import build_real_backend_client
from mcp_call_orchestrator_proxy.orchestrator import CallOrchestrator


@pytest.mark.integration
async def test_proxy_exposes_exact_schema_parity(real_client, real_settings):
    """Schema parity (tools focus): the proxy must expose the backend's tools
    with identical names and original schemas, plus the added backend_status
    tool. (Resources and prompts are forwarded when the backend supports them.)

    This test is deliberately tolerant of backends that only implement the
    tools surface (common for many MCP servers).
    """
    # Direct to backend (ground truth)
    backend_tools = await real_client.list_tools()

    # Through the proxy
    proxy_app = create_proxy_app(settings=real_settings)
    async with Client(proxy_app.mcp) as proxy_client:
        proxy_tools = await proxy_client.list_tools()

        # Best-effort capture of optional surfaces while client is open
        proxy_resources = proxy_prompts = None
        backend_resources = backend_prompts = None
        for surface in ("list_resources", "list_prompts"):
            try:
                b = await getattr(real_client, surface)()
                p = await getattr(proxy_client, surface)()
                if surface == "list_resources":
                    backend_resources = b
                    proxy_resources = p
                else:
                    backend_prompts = b
                    proxy_prompts = p
            except Exception as e:
                if (
                    "method not found" in str(e).lower()
                    or "not found" in str(e).lower()
                ):
                    pass
                else:
                    raise

    proxy_tool_names = {t.name for t in proxy_tools}
    backend_tool_names = {t.name for t in backend_tools}

    # proxy always adds exactly the backend_status tool
    assert "backend_status" in proxy_tool_names
    assert backend_tool_names.issubset(proxy_tool_names)
    assert len(proxy_tool_names) == len(backend_tool_names) + 1

    # Exact schema + description parity for every backend-originated tool
    for bt in backend_tools:
        pt = next((t for t in proxy_tools if t.name == bt.name), None)
        assert pt is not None, f"backend tool {bt.name} missing from proxy"
        assert pt.description == bt.description
        assert pt.inputSchema == bt.inputSchema, f"schema mismatch for {bt.name}"

    # Best-effort parity for resources/prompts (only if both sides returned values)
    if backend_resources is not None and proxy_resources is not None:
        assert {getattr(x, "uri", None) for x in proxy_resources} == {
            getattr(x, "uri", None) for x in backend_resources
        }
    if backend_prompts is not None and proxy_prompts is not None:
        assert {getattr(x, "name", None) for x in proxy_prompts} == {
            getattr(x, "name", None) for x in backend_prompts
        }


@pytest.mark.integration
async def test_real_client_can_call_a_backend_tool(real_client, probe_tool):
    """Basic validation that we can execute a tool on the configured backend."""
    name, args = probe_tool
    result = await real_client.call_tool_mcp(name, args)

    assert result is not None
    assert not result.isError, f"Tool call to {name} failed: {result}"
    # Some backends may return empty content for certain tools; presence of result is enough


@pytest.mark.integration
async def test_real_orchestrator_with_real_client(real_settings, probe_tool):
    """Test that the orchestrator + queue-aware facade work with a real backend."""
    name, args = probe_tool
    upstream = build_real_backend_client(real_settings)

    async with upstream, CallOrchestrator() as orch:
        backend = QueuedMCPBackend(upstream, orch)
        result = await backend.call_tool_mcp(name, args)
        assert result is not None


@pytest.mark.integration
async def test_full_app_with_real_client(real_settings, probe_tool):
    """Drive the entire real stack - real backend, orchestrator,
    and the exposed FastMCP proxy surface - through an in-memory client."""
    name, args = probe_tool
    proxy_app = create_proxy_app(settings=real_settings)

    async with Client(proxy_app.mcp) as client:
        result = await client.call_tool(name, args)
        assert result is not None
        # result.content may be present or is_error False depending on backend tool


@pytest.mark.integration
async def test_concurrent_calls_are_serialized_against_real_backend(
    real_settings, wrap_observer, probe_tool
):
    """The project's core guarantee, proven end-to-end against a real backend.

    Fires many simultaneous tool calls at the exposed proxy surface and
    asserts (a) none hang - they all complete within a generous timeout -
    and (b) the real backend never sees more than `max_concurrent` calls
    at once. With the default `max_concurrent=1` this is a hard invariant
    (`max_active <= 1`), independent of timing, so the test is not flaky.

    Uses a dynamically discovered tool (see probe_tool fixture) so the test
    works against any backend. Drives the proxy through the in-memory
    transport from a single client session; see the multi-session HTTP test
    for the real-HTTP-transport analog.
    """
    name, args = probe_tool
    settings = real_settings
    assert settings.max_concurrent_calls == 1, "test assumes serialized default"

    observer = wrap_observer(build_real_backend_client(settings))
    proxy_app = create_proxy_app(settings=settings, backend_client=observer)

    call_count = 10
    async with Client(proxy_app.mcp) as client:
        results = await asyncio.wait_for(
            asyncio.gather(*(client.call_tool(name, args) for _ in range(call_count))),
            timeout=60.0,
        )

    assert len(results) == call_count
    for result in results:
        assert result is not None

    assert observer.tool_calls >= call_count, (
        f"expected at least {call_count} backend tool calls, saw {observer.tool_calls}"
    )
    assert observer.max_active <= settings.max_concurrent_calls, (
        f"backend saw {observer.max_active} concurrent calls; "
        f"serializer must bound it to {settings.max_concurrent_calls}"
    )


@pytest.mark.integration
async def test_multiple_real_sessions_over_http_do_not_interfere(
    real_settings, serve_over_http, wrap_observer, probe_tool
):
    """The headline promise, proven end-to-end: several independent clients,
    over a real HTTP transport, against a real backend, without interference.

    Serves the proxy over its real uvicorn HTTP transport - one server process,
    one lifespan, one orchestrator, one shared backend connection - and
    connects multiple *separate* client sessions to it. The shared backend is
    still bound to `max_concurrent` (default 1) across all sessions.

    Uses a dynamically discovered probe tool so this works for any backend.
    """
    name, args = probe_tool
    settings = real_settings
    assert settings.max_concurrent_calls == 1, "test assumes serialized default"

    observer = wrap_observer(build_real_backend_client(settings))
    proxy_app = create_proxy_app(settings=settings, backend_client=observer)

    session_count = 3
    calls_per_session = 3

    async def run_session(url: str) -> None:
        async with Client(url) as client:
            for _ in range(calls_per_session):
                result = await client.call_tool(name, args)
                assert result is not None

    async with serve_over_http(proxy_app.mcp) as url:
        await asyncio.wait_for(
            asyncio.gather(*(run_session(url) for _ in range(session_count))),
            timeout=90.0,
        )

    assert observer.tool_calls >= session_count * calls_per_session
    assert observer.max_active <= settings.max_concurrent_calls, (
        f"backend saw {observer.max_active} concurrent calls across sessions; "
        f"serializer must bound it to {settings.max_concurrent_calls}"
    )


@pytest.mark.integration
async def test_backend_status_and_resilience_available(real_settings):
    """Resilience: the proxy is always connectable and exposes backend_status
    (a proxy-native tool) so clients can inspect backend health without
    provoking a failing backend call. This holds even if the backend later
    goes away (tested more thoroughly in component suite with controllable
    backends).
    """
    proxy_app = create_proxy_app(settings=real_settings)

    async with Client(proxy_app.mcp) as client:
        # backend_status must always be listed
        tools = await client.list_tools()
        tool_names = [t.name for t in tools]
        assert "backend_status" in tool_names

        # Can be called successfully
        status = await client.call_tool("backend_status", {})
        assert status is not None
        # The content will contain the snapshot dict (exact shape asserted
        # in unit/component tests). Just ensure it doesn't error here.
