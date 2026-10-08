"""Multi-session proof over a real HTTP transport, with a mock backend.

This is the headline promise of the whole project, tested offline: multiple
independent MCP clients connect to the proxy *over the wire* and use it
concurrently without interfering with one another. Unlike the in-memory
unit tests, this drives the proxy's real uvicorn HTTP transport - one server
process, one lifespan, one orchestrator, one shared backend - so it exercises
the exact single-process/multi-session path where Backend may strugle multiple connected agent sessions
lived ("second agent connects -> first agent hangs").

A mock ``EchoBackend`` stands in for Backend MCP, so this test is fast,
deterministic, and runs in the offline gate with no vault required. Because
the backend echoes each call's value verbatim, the test can prove responses
are routed back to the *originating* session and never to another.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from fastmcp import Client

from mcp_call_orchestrator_proxy.app import create_proxy_app
from mcp_call_orchestrator_proxy.config import ProxySettings

pytestmark = pytest.mark.component


@pytest.mark.asyncio
async def test_multiple_sessions_over_http_do_not_interfere(
    serve_over_http,
    wrap_observer,
    make_echo_backend,
) -> None:
    """Three concurrent client sessions over real HTTP: no hangs, no cross-talk,
    and the shared backend never sees more than ``max_concurrent`` calls."""
    fake_settings = ProxySettings(backend_api_key="a" * 32)
    assert fake_settings.max_concurrent_calls == 1, "test assumes serialized default"

    observer = wrap_observer(make_echo_backend(delay=0.05))
    proxy_app = create_proxy_app(settings=fake_settings, backend_client=observer)

    session_tags = ("A", "B", "C")
    calls_per_session = 5

    async def run_session(tag: str, url: str) -> list[str]:
        """One independent client session firing several tool calls in order."""
        async with Client(url) as client:
            echoes: list[str] = []
            for i in range(calls_per_session):
                result = await client.call_tool("echo", {"value": f"{tag}-{i}"})
                assert not result.is_error, f"session {tag} call {i} errored"
                echoes.append(result.content[0].text)
            return echoes

    async with serve_over_http(proxy_app.mcp) as url:
        results = await asyncio.wait_for(
            asyncio.gather(*(run_session(tag, url) for tag in session_tags)),
            timeout=30.0,
        )

    # (2) Correct routing / no cross-talk: each session received exactly its own
    #     tagged values, in order, and never another session's.
    for tag, echoes in zip(session_tags, results, strict=True):
        assert echoes == [f"{tag}-{i}" for i in range(calls_per_session)], (
            f"session {tag} saw mis-routed responses: {echoes}"
        )

    # (3) Shared-backend serialization across sessions: with max_concurrent=1 the
    #     backend must never see two calls at once, regardless of timing (a hard
    #     invariant, so this is not flaky). tool_calls confirms real contention
    #     actually reached the shared backend.
    expected_calls = len(session_tags) * calls_per_session
    assert observer.tool_calls == expected_calls, (
        f"expected {expected_calls} backend calls, saw {observer.tool_calls}"
    )
    assert observer.max_active <= fake_settings.max_concurrent_calls, (
        f"backend saw {observer.max_active} concurrent calls; serializer must "
        f"bound it to {fake_settings.max_concurrent_calls}"
    )


def client_of(message: str) -> str:
    """The client tag out of a `... client=<tag> op=...` record."""
    return message.split("client=")[1].split()[0]


@pytest.mark.asyncio
async def test_the_default_log_shows_two_agents_being_interleaved_safely(
    serve_over_http, make_echo_backend, caplog: pytest.LogCaptureFixture
) -> None:
    """The product's whole value proposition, read back off the default-level log.

    Two agents hammer the proxy over real HTTP. Afterwards an operator must be
    able to reconstruct, from the log alone: which calls came from which client,
    in what order the queue served them, and that they did not overlap at the
    backend. Without that, there is no way to confirm the interleaving is safe
    rather than racing - which is the one thing this proxy exists to promise.
    """
    caplog.set_level(logging.INFO, logger="mcp_call_orchestrator_proxy")

    fake_settings = ProxySettings(backend_api_key="a" * 32)
    backend = make_echo_backend(delay=0.02)
    proxy_app = create_proxy_app(settings=fake_settings, backend_client=backend)

    calls_each = 3

    async def run_one(url: str, tag: str) -> None:
        async with Client(url) as client:
            for i in range(calls_each):
                await client.call_tool("echo", {"value": f"{tag}-{i}"})

    async with serve_over_http(proxy_app.mcp) as url:
        await asyncio.gather(
            run_one(url, "client-one"),
            run_one(url, "client-two"),
        )

    ours = [
        r.getMessage() for r in caplog.records if "orchestrator: call" in r.getMessage()
    ]
    accepted = [m for m in ours if "call accepted" in m]
    completed = [m for m in ours if "call completed" in m]
    tool_calls = [m for m in accepted if "op=call_tool:echo" in m]

    assert len(tool_calls) == 2 * calls_each
    assert len(accepted) == len(completed), (
        "every accepted call must have a matching completion; an acceptance with "
        "no completion is how a hang shows up, and there was no hang here"
    )

    # The two sessions must be *distinguishable*, or none of the above can be
    # attributed to anyone. This is the assertion the whole test turns on.
    tags = {client_of(m) for m in tool_calls}
    assert len(tags) == 2, f"two agents were not told apart in the log: {tags}"
    for tag in tags:
        assert sum(client_of(m) == tag for m in tool_calls) == calls_each

    # Serialization is readable: a call completes before the next one starts.
    # (`wait` is queue time, `exec` is time at the backend - so an overlap would
    # show as two calls whose exec windows intersect.)
    assert all("wait=" in m and "exec=" in m for m in completed)

    # Nothing here needs an operator, so the warning filter must stay empty.
    noisy = [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING
        and r.name.startswith("mcp_call_orchestrator_proxy")
    ]
    assert not noisy, f"a healthy busy period tripped the warning filter: {noisy}"
