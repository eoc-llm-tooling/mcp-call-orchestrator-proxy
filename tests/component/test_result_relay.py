"""A tool result reaches its caller as the backend sent it (OCP-C-002).

The backend here is the probe server, whose ``mismatched_output`` tool returns
structured content that its own declared output schema rejects. Checking a
result against that schema is the job of whoever consumes it; the proxy only
relays it, so the check must not run there, and a backend's schema bug must not
read as a broken connection under every other client's calls (OCP-N-001).

Each test runs against the probe reached over stdio, which the proxy spawns
itself, and over HTTP at both protocol versions.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.server.providers.proxy import ProxyClient

from mcp_call_orchestrator_proxy.app import create_proxy_app
from mcp_call_orchestrator_proxy.config import ProxySettings

sys.path.insert(0, str(Path(__file__).parent.parent / "support"))
from probe_backend_server import MISMATCHED_OUTPUT

pytestmark = pytest.mark.component

BACKENDS = pytest.mark.parametrize("backend_kind", ["stdio", "http-2025", "http-2026"])


def _backend_config(
    backend_kind: str,
    spawn_backend: Callable[..., Any],
    backend_config_file: Callable[[str], str],
    stdio_backend_config_file: Callable[[dict[str, str] | None], str],
) -> str:
    if backend_kind == "stdio":
        return stdio_backend_config_file(None)
    backend = spawn_backend(legacy=backend_kind == "http-2025")
    return backend_config_file(backend.url)


def _status(result: Any) -> dict[str, Any]:
    data = result.data
    assert isinstance(data, dict), f"unexpected backend_status payload: {result}"
    return data


@pytest.mark.asyncio
@BACKENDS
async def test_a_result_off_its_output_schema_reaches_the_caller_unchanged(
    spawn_backend,
    backend_config_file,
    stdio_backend_config_file,
    serve_over_http,
    caplog: pytest.LogCaptureFixture,
    backend_kind: str,
) -> None:
    """Client A gets the backend's structured content as sent, and client B's
    call straight after it is served on the same connection, with no retry.

    Client A is a ``ProxyClient``, whose session hands results back without
    checking them, so the test sees exactly what the proxy relayed. With the
    proxy's default reconnect backoff, a connection torn down by A's call would
    still be down when B's call arrives.
    """
    config = _backend_config(
        backend_kind, spawn_backend, backend_config_file, stdio_backend_config_file
    )
    proxy_app = create_proxy_app(settings=ProxySettings(backend_config=config))

    with caplog.at_level(logging.WARNING, logger="mcp_call_orchestrator_proxy"):
        async with (
            serve_over_http(proxy_app.mcp) as url,
            ProxyClient(url) as client_a,
            Client(url) as client_b,
        ):
            before = _status(await client_b.call_tool("backend_status", {}))

            result = await client_a.call_tool_mcp("mismatched_output", {})
            assert not result.is_error, result.content
            assert result.structured_content == MISMATCHED_OUTPUT

            echoed = await client_b.call_tool(
                "echo", {"value": "B's call"}, raise_on_error=False
            )
            assert not echoed.is_error, echoed.content
            assert echoed.content[0].text == "B's call"

            after = _status(await client_b.call_tool("backend_status", {}))

    assert after["connected"] is True
    assert after["last_connected_at"] == before["last_connected_at"]
    assert after["consecutive_failures"] == 0
    assert [r.getMessage() for r in caplog.records] == []


@pytest.mark.asyncio
@BACKENDS
async def test_a_checking_client_rejects_the_relayed_result_itself(
    spawn_backend,
    backend_config_file,
    stdio_backend_config_file,
    serve_over_http,
    backend_kind: str,
) -> None:
    """A client that checks results against the declared output schema, as a
    plain ``fastmcp.Client`` does, finds the mismatch in the backend's own
    content: the proxy relays the tool's schema and its result unchanged."""
    config = _backend_config(
        backend_kind, spawn_backend, backend_config_file, stdio_backend_config_file
    )
    proxy_app = create_proxy_app(settings=ProxySettings(backend_config=config))

    async with serve_over_http(proxy_app.mcp) as url, Client(url) as client:
        with pytest.raises(
            RuntimeError,
            match=r"Invalid structured content returned by tool mismatched_output",
        ):
            await client.call_tool_mcp("mismatched_output", {})
