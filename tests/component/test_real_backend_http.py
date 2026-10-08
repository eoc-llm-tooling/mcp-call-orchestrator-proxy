"""The proxy's guarantees, proven against a real MCP server over real HTTP.

The other component tests inject an in-process mock as the backend. That mock
speaks no protocol and crosses no transport, so it can only show that the proxy
agrees with *our own* idea of MCP - it shares any misconception the proxy has.
Here the backend is FastMCP's server implementation running in a child process
this test starts and stops (``tests/support/probe_backend_server.py``), reached
over real HTTP, and the proxy is pointed at it through a real ``mcpServers``
JSON file - so the production ``client.py`` path (config parsing, transport
construction, ``fastmcp.Client``) is exercised too, with nothing injected.

Two properties make the concurrency claims worth something here:

- The backend counts its own in-flight tool calls (``call_stats``), so
  serialization is measured **at the backend**, not by a wrapper living in the
  proxy's own process.
- ``test_backend_is_seen_running_calls_concurrently_when_allowed`` is the
  control: it shows that meter reading *above* 1 when the proxy is configured
  to allow it. Without it, "the backend never saw two calls at once" could just
  mean the meter is broken.

Still offline: no network, no operator backend. The whole thing is a child
process on loopback.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pytest
from fastmcp import Client

from mcp_call_orchestrator_proxy.app import create_proxy_app
from mcp_call_orchestrator_proxy.config import ProxySettings

pytestmark = pytest.mark.component

# Fast reconnects: the supervisor's defaults are tuned for a real deployment,
# where a backend that has gone away is best left alone for a second or two.
_RESILIENCE_TUNING: dict[str, Any] = {
    "connect_timeout_seconds": 5.0,
    "reconnect_initial_backoff_seconds": 0.05,
    "reconnect_max_backoff_seconds": 0.2,
    "backend_health_poll_seconds": 0.05,
}


async def read_backend_stats(backend_url: str) -> dict[str, int]:
    """Ask the backend directly - not through the proxy - what it served."""
    async with Client(backend_url) as direct:
        result = await direct.call_tool("call_stats", {})
    data = result.data
    assert isinstance(data, dict), f"unexpected call_stats payload: {result}"
    return data


def status_payload(result: Any) -> dict[str, Any]:
    """Pull the backend_status dict out of a CallToolResult, tolerant of shape."""
    if isinstance(result.data, dict):
        return result.data
    structured = result.structured_content or {}
    inner = structured.get("result")
    return inner if isinstance(inner, dict) else structured


async def wait_for_connected(client: Client, expected: bool) -> None:
    """Block until the proxy reports the backend up/down, or fail the test."""
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        snapshot = status_payload(await client.call_tool("backend_status", {}))
        if bool(snapshot["connected"]) is expected:
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"proxy never reported connected={expected}; last: {snapshot}")


@pytest.mark.asyncio
async def test_concurrent_sessions_are_serialized_at_a_real_backend(
    spawn_backend, backend_config_file, serve_over_http
) -> None:
    """The headline promise, against a real server: several client sessions hammer
    the proxy at once; the backend still serves their calls strictly one at a time,
    and no session ever receives another session's result."""
    backend = spawn_backend()
    settings = ProxySettings(backend_config=backend_config_file(backend.url))
    assert settings.max_concurrent_calls == 1, "test assumes the serialized default"

    proxy_app = create_proxy_app(settings=settings)

    session_tags = ("A", "B", "C")
    calls_per_session = 4

    async def run_session(tag: str, url: str) -> list[str]:
        """One independent client session firing several tool calls in order."""
        async with Client(url) as client:
            echoes: list[str] = []
            for i in range(calls_per_session):
                result = await client.call_tool(
                    "slow_echo", {"value": f"{tag}-{i}", "delay_seconds": 0.1}
                )
                assert not result.is_error, f"session {tag} call {i} errored"
                echoes.append(result.content[0].text)
            return echoes

    async with serve_over_http(proxy_app.mcp) as url:
        results = await asyncio.wait_for(
            asyncio.gather(*(run_session(tag, url) for tag in session_tags)),
            timeout=60.0,
        )
        # Read the backend's own account while the proxy is still up: stopping it
        # means cancelling its uvicorn task, and a fresh outbound connection made
        # in that event loop afterwards does not reliably come up.
        stats = await read_backend_stats(backend.url)

    # No cross-talk: each session got its own tagged values back, in order, and
    # never another session's - the mis-routing this project exists to prevent.
    for tag, echoes in zip(session_tags, results, strict=True):
        assert echoes == [f"{tag}-{i}" for i in range(calls_per_session)], (
            f"session {tag} saw mis-routed responses: {echoes}"
        )

    # Serialization, as counted by the backend itself.
    expected_calls = len(session_tags) * calls_per_session
    assert stats["total_calls"] == expected_calls, (
        f"backend served {stats['total_calls']} calls, expected {expected_calls} - "
        "the load never reached it, so serialization was not exercised"
    )
    assert stats["peak_active"] == 1, (
        f"backend ran {stats['peak_active']} calls at once; with "
        f"max_concurrent_calls=1 it must never see more than one"
    )


@pytest.mark.asyncio
async def test_backend_is_seen_running_calls_concurrently_when_allowed(
    spawn_backend, backend_config_file, serve_over_http
) -> None:
    """The control for the test above: raise the limit and the very same meter
    reads above 1. Proves ``peak_active == 1`` there is the serializer's doing,
    not a meter that cannot count."""
    backend = spawn_backend()
    settings = ProxySettings(
        backend_config=backend_config_file(backend.url), max_concurrent_calls=3
    )

    proxy_app = create_proxy_app(settings=settings)

    async def call(client: Client, value: str) -> None:
        result = await client.call_tool(
            "slow_echo", {"value": value, "delay_seconds": 0.3}
        )
        assert not result.is_error

    async with serve_over_http(proxy_app.mcp) as url, Client(url) as client:
        await asyncio.wait_for(
            asyncio.gather(*(call(client, f"v{i}") for i in range(6))), timeout=60.0
        )
        stats = await read_backend_stats(backend.url)

    assert 1 < stats["peak_active"] <= 3, (
        f"backend peaked at {stats['peak_active']} concurrent calls; expected it to "
        "exceed 1 (the meter can see concurrency) but stay within the limit of 3"
    )


@pytest.mark.asyncio
async def test_proxy_mirrors_a_real_backend_tool_schemas_exactly(
    spawn_backend, backend_config_file, serve_over_http
) -> None:
    """Schema parity against a server whose schemas we did not hand-write: every
    backend tool appears through the proxy under its own name and its own exact
    input schema, plus the proxy's own ``backend_status`` and nothing else."""
    backend = spawn_backend()
    settings = ProxySettings(backend_config=backend_config_file(backend.url))
    proxy_app = create_proxy_app(settings=settings)

    async with Client(backend.url) as direct:
        backend_tools = {t.name: t.inputSchema for t in await direct.list_tools()}
    assert backend_tools, "probe backend exposed no tools"

    async with serve_over_http(proxy_app.mcp) as url, Client(url) as client:
        proxied_tools = {t.name: t.inputSchema for t in await client.list_tools()}

    assert set(proxied_tools) == set(backend_tools) | {"backend_status"}, (
        "the proxied tool surface must be the backend's, plus backend_status"
    )
    for name, schema in backend_tools.items():
        assert proxied_tools[name] == schema, f"schema for {name!r} was not mirrored"


@pytest.mark.asyncio
async def test_proxy_survives_a_real_backend_dying_and_coming_back(
    spawn_backend, backend_config_file, serve_over_http
) -> None:
    """A real backend process is killed outright and later restarted at the same
    address. The proxy stays connectable throughout, reports the outage, and
    reconnects on its own - no restart of the proxy, no client intervention."""
    backend = spawn_backend()
    settings = ProxySettings(
        backend_config=backend_config_file(backend.url), **_RESILIENCE_TUNING
    )
    proxy_app = create_proxy_app(settings=settings)

    async with serve_over_http(proxy_app.mcp) as url, Client(url) as client:
        echoed = await client.call_tool("echo", {"value": "before"})
        assert echoed.content[0].text == "before"

        backend.kill()

        # The proxy is still there to be asked - that is the promise. The failing
        # call is what tells it the connection is gone (break detection is
        # network-silent: nothing pings the backend).
        with pytest.raises(Exception):  # noqa: B017 - any error, as long as it is one
            await client.call_tool("echo", {"value": "during"})
        await wait_for_connected(client, expected=False)

        # The backend comes back at the same address; the supervisor notices.
        backend.start()
        await wait_for_connected(client, expected=True)

        echoed = await client.call_tool("echo", {"value": "after"})
        assert echoed.content[0].text == "after"


@pytest.mark.asyncio
async def test_a_hung_backend_call_does_not_wedge_the_queue(
    spawn_backend, backend_config_file, serve_over_http
) -> None:
    """A call the backend never answers is timed out and its slot released, so the
    queue behind it still runs. Otherwise a single hung call would take the whole
    proxy down with it - the failure this project exists to prevent."""
    backend = spawn_backend()
    settings = ProxySettings(
        backend_config=backend_config_file(backend.url),
        call_timeout_seconds=2.0,
        **_RESILIENCE_TUNING,
    )
    proxy_app = create_proxy_app(settings=settings)

    async with serve_over_http(proxy_app.mcp) as url, Client(url) as client:
        with pytest.raises(Exception):  # noqa: B017 - any error, as long as it is one
            await asyncio.wait_for(client.call_tool("hang", {}), timeout=20.0)

        # The queue is still usable. A hang can break the backend connection (a
        # call that never answered is indistinguishable from a dead one), so give
        # the supervisor its moment before insisting the queue works.
        await wait_for_connected(client, expected=True)
        echoed = await client.call_tool("echo", {"value": "after the hang"})
        assert echoed.content[0].text == "after the hang"


@pytest.mark.asyncio
async def test_a_warnings_only_filter_shows_the_outage_and_the_recovery(
    spawn_backend,
    backend_config_file,
    serve_over_http,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Severity alone must tell the operator whether something needs them.

    This is the log an operator actually reads: `journalctl -p warning`, or an
    alert rule, during a real backend outage. It has to tell the whole story -
    the backend went away, and it came back. A recovery logged below the filter
    is a recovery the operator never sees, and every transient blip is left
    looking like an unresolved incident.

    The backend really dies here (SIGKILL) and really returns, so the records
    under test are the ones a production outage produces.
    """
    caplog.set_level(logging.DEBUG, logger="mcp_call_orchestrator_proxy")

    backend = spawn_backend()
    settings = ProxySettings(
        backend_config=backend_config_file(backend.url), **_RESILIENCE_TUNING
    )
    proxy_app = create_proxy_app(settings=settings)

    async with serve_over_http(proxy_app.mcp) as url, Client(url) as client:
        await client.call_tool("echo", {"value": "before"})
        backend.kill()
        with pytest.raises(Exception):  # noqa: B017 - the call must fail; how is not the point
            await client.call_tool("echo", {"value": "during"})
        await wait_for_connected(client, expected=False)
        backend.start()
        await wait_for_connected(client, expected=True)
        await client.call_tool("echo", {"value": "after"})

    ours = [
        r for r in caplog.records if r.name.startswith("mcp_call_orchestrator_proxy")
    ]
    visible = [r.getMessage() for r in ours if r.levelno >= logging.WARNING]

    assert any("backend connection failed" in m for m in visible), (
        f"the outage is invisible to a warnings-only filter; it showed: {visible}"
    )
    assert any("reconnected" in m for m in visible), (
        "the recovery is invisible to a warnings-only filter - the operator sees "
        f"the backend go down and never sees it come back; it showed: {visible}"
    )

    # An outage the proxy absorbs by design must not be reported as an unhandled
    # fault. A stack trace here would put the routine, self-healed case at the
    # same severity as a genuine bug - which is exactly what makes operators stop
    # reading the level at all.
    with_tracebacks = [r.getMessage() for r in ours if r.exc_info is not None]
    assert not with_tracebacks, (
        f"a self-healed backend outage logged a traceback: {with_tracebacks}"
    )
