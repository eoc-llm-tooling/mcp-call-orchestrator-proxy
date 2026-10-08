"""The proxy's guarantees, proven against a real MCP server reached over stdio.

``tests/component/test_real_backend_http.py`` proves the same guarantees for an
HTTP backend, launched and killed by the test itself (``spawn_backend``). Here
the backend is the same probe server (``tests/support/probe_backend_server.py
--stdio``), but the proxy is the one that spawns it, keeps it alive across
reconnects, and kills it - that ownership is what a stdio backend actually
needs from this proxy (docs/roadmap.md, "front stdio backends"), so it is what
these tests check, rather than repeating the HTTP suite's serialization claims
against a different transport.

Still offline: no network, no operator backend. The whole thing is a child
process on loopback stdio pipes.
"""

from __future__ import annotations

import asyncio
import os
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


def _status_payload(result: Any) -> dict[str, Any]:
    """Pull the backend_status dict out of a CallToolResult, tolerant of shape."""
    if isinstance(result.data, dict):
        return result.data
    structured = result.structured_content or {}
    inner = structured.get("result")
    return inner if isinstance(inner, dict) else structured


async def _wait_for_connected(client: Client, expected: bool) -> None:
    """Block until the proxy reports the backend up/down, or fail the test."""
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        snapshot = _status_payload(await client.call_tool("backend_status", {}))
        if bool(snapshot["connected"]) is expected:
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"proxy never reported connected={expected}; last: {snapshot}")


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _wait_until_dead(pid: int, *, timeout: float = 10.0) -> None:
    """Poll for a pid's death - reaping a killed subprocess isn't instantaneous."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_alive(pid):
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"pid {pid} was still alive {timeout}s after the proxy let go of it")


@pytest.mark.asyncio
async def test_proxy_fronts_a_real_stdio_backend_with_exact_schema_parity(
    stdio_backend_config_file, serve_over_http
) -> None:
    """A backend reached over stdio (command/args, no URL) gets the same exact
    tool-schema mirroring as an HTTP one, through the same production client.py
    path - proving the basic stdio route needs no proxy-side special-casing."""
    settings = ProxySettings(backend_config=stdio_backend_config_file())
    proxy_app = create_proxy_app(settings=settings)

    async with serve_over_http(proxy_app.mcp) as url, Client(url) as client:
        tools = {t.name for t in await client.list_tools()}
        assert tools == {
            "echo",
            "slow_echo",
            "hang",
            "call_stats",
            "pid",
            "crash",
            "env_var",
            "backend_status",
        }
        echoed = await client.call_tool("echo", {"value": "hello over stdio"})
        assert echoed.content[0].text == "hello over stdio"


@pytest.mark.asyncio
async def test_proxy_kills_the_stdio_backend_process_when_it_stops(
    stdio_backend_config_file,
) -> None:
    """Stopping the proxy must not leave the backend's child process running.

    Uses the in-memory ``Client(proxy_app.mcp)`` transport rather than
    ``serve_over_http``: that harness stops the proxy by cancelling its uvicorn
    task, which is not the same as a clean lifespan shutdown. One
    ``async with Client(proxy_app.mcp) as client:`` block is exactly one full
    lifespan cycle (connect -> ... -> ``orchestrator.stop()``), which is the
    boundary this test needs to be meaningful.
    """
    settings = ProxySettings(backend_config=stdio_backend_config_file())
    proxy_app = create_proxy_app(settings=settings)

    async with Client(proxy_app.mcp) as client:
        result = await client.call_tool("pid", {})
        backend_pid = result.data
        assert isinstance(backend_pid, int)
        assert _pid_is_alive(backend_pid)

    await _wait_until_dead(backend_pid)


@pytest.mark.asyncio
async def test_proxy_survives_a_stdio_backend_crashing_and_reconnects(
    stdio_backend_config_file, serve_over_http
) -> None:
    """The backend process dies mid-session (``crash``, no port to kill it by).
    The proxy stays connectable, reports the outage, and reconnects with a
    genuinely new process - no restart of the proxy, no client intervention."""
    settings = ProxySettings(
        backend_config=stdio_backend_config_file(), **_RESILIENCE_TUNING
    )
    proxy_app = create_proxy_app(settings=settings)

    async with serve_over_http(proxy_app.mcp) as url, Client(url) as client:
        first_pid = (await client.call_tool("pid", {})).data
        assert isinstance(first_pid, int)

        with pytest.raises(Exception):  # noqa: B017 - any error, as long as it is one
            await client.call_tool("crash", {})
        await _wait_for_connected(client, expected=False)

        await _wait_for_connected(client, expected=True)
        second_pid = (await client.call_tool("pid", {})).data
        assert isinstance(second_pid, int)

    assert second_pid != first_pid, (
        "reconnect must spawn a genuinely new process, not reuse the dead one"
    )
    await _wait_until_dead(first_pid)


@pytest.mark.asyncio
async def test_stdio_backend_env_is_explicit_not_inherited_from_the_proxy(
    stdio_backend_config_file, serve_over_http, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child's environment is what the config declares, not a copy of the
    proxy's own environment and secrets - the mcp SDK's own safe-subset default
    (docs/roadmap.md), exercised here end-to-end through the real client.py
    path rather than merely asserted."""
    monkeypatch.setenv("MCP_PROXY_TEST_SECRET", "shh")
    settings = ProxySettings(
        backend_config=stdio_backend_config_file(env={"FOO": "bar"})
    )
    proxy_app = create_proxy_app(settings=settings)

    async with serve_over_http(proxy_app.mcp) as url, Client(url) as client:
        leaked = await client.call_tool("env_var", {"name": "MCP_PROXY_TEST_SECRET"})
        declared = await client.call_tool("env_var", {"name": "FOO"})

    assert leaked.data is None, "the proxy's own environment must not reach the child"
    assert declared.data == "bar", "the config's own declared env must reach the child"
