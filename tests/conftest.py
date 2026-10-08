"""Shared test harness for driving the proxy over a real HTTP transport.

The in-memory ``Client(proxy_app.mcp)`` transport used by the unit tests
cannot model the multi-session promise: it enters the server lifespan and
starts a fresh ``_mcp_server.run()`` loop *per client connection*, so two
in-memory clients get two independent orchestrators and two backend
connections. Only a single real HTTP server process - one lifespan, one
orchestrator, one shared backend, serving many sessions - exercises the
path where cross-session interference and shared-backend serialization
actually happen.

This module provides that harness (``serve_over_http``) and the two kinds of
backend the offline tiers put behind it:

- **In-process mocks** (``ConcurrencyObserver``, ``EchoBackend``) - objects
  satisfying the ``MCPBackendClient`` Protocol, injected straight into
  ``create_proxy_app``. Fast and precisely controllable, but they speak no
  protocol over no transport, so they can only show that the proxy agrees
  with our own idea of MCP.
- **A real MCP server in a child process** (``spawn_backend``) - FastMCP's
  server, our tools (``tests/support/probe_backend_server.py``), reached over
  real HTTP through the proxy's *production* client path. The test owns its
  lifecycle: it starts it, can kill it, and can bring it back.

Both stay offline. Helpers are exposed as fixtures so every suite - including
the integration suite with its own ``conftest.py`` - can use them without
cross-directory imports.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import IO, Any

import pytest
from fastmcp import FastMCP
from mcp import types as mcp_types

from mcp_call_orchestrator_proxy.interfaces import MCPBackendClient

PROBE_BACKEND_SERVER = Path(__file__).parent / "support" / "probe_backend_server.py"


@contextlib.asynccontextmanager
async def serve_proxy_over_http(mcp: FastMCP) -> AsyncIterator[str]:
    """Serve a FastMCP server over a real loopback HTTP port for the block.

    Binds an ephemeral ``127.0.0.1`` socket, runs the server's real uvicorn
    HTTP transport in a background task (a single lifespan entry, shared by
    every session that connects), waits until it accepts connections, and
    yields the base MCP URL. On exit the server task is cancelled and drained.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    # Do not call sock.listen(): uvicorn's create_server(sock=...) handles it.
    port = sock.getsockname()[1]

    server_task = asyncio.create_task(
        mcp.run_http_async(
            host="127.0.0.1",
            port=port,
            sockets=[sock],
            transport="http",
            show_banner=False,
            log_level="warning",
        )
    )

    try:
        await _wait_until_accepting("127.0.0.1", port, timeout=10.0)
        yield f"http://127.0.0.1:{port}/mcp/"
    finally:
        server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server_task
        sock.close()


async def _wait_until_accepting(host: str, port: int, *, timeout: float) -> None:
    """Poll a TCP connect until the server accepts, or fail after ``timeout``.

    uvicorn only starts accepting once the server's single lifespan has
    entered, so a successful connect means the backend connection is open
    and the orchestrator's dispatcher is running.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            reader_sock = socket.create_connection((host, port), timeout=0.1)
        except OSError:
            if loop.time() >= deadline:
                raise TimeoutError(
                    f"proxy HTTP server did not come up on {host}:{port} "
                    f"within {timeout}s"
                ) from None
            await asyncio.sleep(0.05)
        else:
            reader_sock.close()
            return


class ConcurrencyObserver:
    """Wraps a backend client, recording peak concurrent in-flight tool calls.

    Sits *between* the orchestrator and the real backend (injected as the
    proxy's ``backend_client``), so ``max_active`` reflects exactly what the
    orchestrator lets reach the backend, not what clients requested. Every
    method delegates to the wrapped client; only ``call_tool_mcp`` is
    instrumented, since that is the operation the serializer must protect.
    """

    def __init__(self, inner: MCPBackendClient) -> None:
        self._inner = inner
        self.active = 0
        self.max_active = 0
        self.tool_calls = 0
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> ConcurrencyObserver:
        await self._inner.__aenter__()
        return self

    async def __aexit__(
        self, exc_type: object, exc_val: object, exc_tb: object
    ) -> None:
        await self._inner.__aexit__(exc_type, exc_val, exc_tb)

    async def initialize(self) -> mcp_types.InitializeResult:
        return await self._inner.initialize()

    async def ping(self) -> bool:
        return await self._inner.ping()

    async def list_tools(self) -> list[mcp_types.Tool]:
        return await self._inner.list_tools()

    async def call_tool_mcp(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        meta: dict[str, Any] | None = None,
    ) -> mcp_types.CallToolResult:
        async with self._lock:
            self.active += 1
            self.tool_calls += 1
            self.max_active = max(self.max_active, self.active)
        try:
            return await self._inner.call_tool_mcp(name, arguments, meta=meta)
        finally:
            async with self._lock:
                self.active -= 1

    async def list_resources(self) -> list[mcp_types.Resource]:
        return await self._inner.list_resources()

    async def list_resource_templates(self) -> list[mcp_types.ResourceTemplate]:
        return await self._inner.list_resource_templates()

    async def read_resource(
        self, uri: str
    ) -> list[mcp_types.TextResourceContents | mcp_types.BlobResourceContents]:
        return await self._inner.read_resource(uri)

    async def list_prompts(self) -> list[mcp_types.Prompt]:
        return await self._inner.list_prompts()

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> mcp_types.GetPromptResult:
        return await self._inner.get_prompt(name, arguments)


ECHO_SCHEMA = {
    "type": "object",
    "properties": {
        "value": {"type": "string", "description": "A string echoed back verbatim."},
    },
    "required": ["value"],
}


class EchoBackend:
    """A mock ``MCPBackendClient`` exposing one ``echo`` tool.

    ``echo`` returns its ``value`` argument verbatim after an optional delay.
    The verbatim echo is what lets a multi-session test detect mis-routing:
    if session A ever receives session B's value, a response was delivered to
    the wrong transport - the exact bug this project exists to prevent. The
    delay widens the window in which calls from different sessions overlap at
    the backend, so the serializer is genuinely exercised.
    """

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay

    async def __aenter__(self) -> EchoBackend:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def initialize(self) -> mcp_types.InitializeResult:
        return mcp_types.InitializeResult(
            protocolVersion="2025-06-18",
            capabilities=mcp_types.ServerCapabilities(),
            serverInfo=mcp_types.Implementation(name="echo", version="0"),
        )

    async def ping(self) -> bool:
        return True

    async def list_tools(self) -> list[mcp_types.Tool]:
        return [
            mcp_types.Tool(
                name="echo",
                description="Echo the given value back verbatim.",
                inputSchema=ECHO_SCHEMA,
            )
        ]

    async def call_tool_mcp(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        meta: dict[str, Any] | None = None,
    ) -> mcp_types.CallToolResult:
        if self.delay:
            await asyncio.sleep(self.delay)
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


class SpawnedBackend:
    """A real MCP server, running as a child process on a fixed loopback port.

    The port is fixed (rather than chosen by the child) so that ``kill()`` and a
    later ``start()`` put the backend back at the *same* address - which is what
    lets a test model a backend that dies and returns, with the proxy's own
    connection details unchanged throughout.
    """

    def __init__(self, port: int) -> None:
        self.port = port
        self.url = f"http://127.0.0.1:{port}/mcp/"
        self._proc: subprocess.Popen[bytes] | None = None
        self._log: IO[bytes] | None = None

    def start(self, *, timeout: float = 30.0) -> None:
        """Spawn the server and return once it is accepting connections."""
        if self._proc is not None:
            raise RuntimeError("backend already running")

        # Not a context manager (SIM115) on purpose: the child writes to this for
        # as long as it runs, so the file's owner is this object, which closes it
        # in kill(). A pipe would risk filling and blocking the child instead.
        self._log = tempfile.TemporaryFile()  # noqa: SIM115
        self._proc = subprocess.Popen(
            [sys.executable, str(PROBE_BACKEND_SERVER), "--port", str(self.port)],
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )
        self._wait_until_serving(timeout=timeout)

    def kill(self) -> None:
        """Kill the server outright, as a crashing backend would go. Idempotent.

        ``SIGKILL``, not ``SIGTERM``: a backend that dies does not get to close
        its sockets politely, and the proxy must cope with the rude version.
        """
        if self._proc is None:
            return
        self._proc.kill()
        self._proc.wait(timeout=10)
        self._proc = None
        if self._log is not None:
            self._log.close()
            self._log = None

    def output(self) -> str:
        """Whatever the child has written so far (its log, for diagnostics)."""
        if self._log is None:
            return ""
        self._log.seek(0)
        return self._log.read().decode(errors="replace")

    def _wait_until_serving(self, *, timeout: float) -> None:
        """Poll a TCP connect until the child accepts, or fail with its output."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            assert self._proc is not None
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"probe backend exited immediately with code "
                    f"{self._proc.returncode}:\n{self.output()}"
                )
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.1).close()
            except OSError:
                time.sleep(0.05)
            else:
                return
        raise TimeoutError(
            f"probe backend did not start serving on port {self.port} within "
            f"{timeout}s:\n{self.output()}"
        )


def _free_loopback_port() -> int:
    """Reserve a free ephemeral loopback port by binding and releasing it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


@pytest.fixture
def spawn_backend() -> Iterator[Callable[[], SpawnedBackend]]:
    """Return a factory that starts a real MCP server in a child process.

    Every server it starts is killed at the end of the test, including one a
    test has already killed and restarted itself.
    """
    started: list[SpawnedBackend] = []

    def _spawn() -> SpawnedBackend:
        backend = SpawnedBackend(_free_loopback_port())
        backend.start()
        started.append(backend)
        return backend

    yield _spawn

    for backend in started:
        backend.kill()


@pytest.fixture
def backend_config_file(tmp_path: Path) -> Callable[[str], str]:
    """Return a factory writing a real ``mcpServers`` JSON file for a backend URL.

    Configuring the proxy this way is the point: it reaches the spawned backend
    through ``client.py``'s production path - the same JSON parsing, transport
    construction and ``fastmcp.Client`` an operator gets - rather than through an
    injected test double.
    """

    def _write(url: str) -> str:
        path = tmp_path / "backend.json"
        path.write_text(
            json.dumps(
                {"mcpServers": {"probe-backend": {"url": url, "transport": "http"}}}
            )
        )
        return str(path)

    return _write


@pytest.fixture
def stdio_backend_config_file(tmp_path: Path) -> Callable[[dict[str, str] | None], str]:
    """Return a factory writing an ``mcpServers`` JSON for a stdio-launched backend.

    Unlike ``backend_config_file``, there is no matching ``spawn_backend``-style
    lifecycle fixture: for a stdio backend, the *proxy itself* spawns and kills
    the child process (via ``client.py`` and ``ResilientBackend``) - that
    ownership is exactly what the stdio tests are checking, so the harness only
    needs to hand it a config, not manage the process on the test's behalf.
    """

    def _write(env: dict[str, str] | None = None) -> str:
        path = tmp_path / "backend.json"
        path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "probe-backend": {
                            "command": sys.executable,
                            "args": [str(PROBE_BACKEND_SERVER), "--stdio"],
                            "env": env or {},
                        }
                    }
                }
            )
        )
        return str(path)

    return _write


@pytest.fixture
def serve_over_http() -> Callable[
    [FastMCP], contextlib.AbstractAsyncContextManager[str]
]:
    """Return the ``serve_proxy_over_http`` context manager for use in a test."""
    return serve_proxy_over_http


@pytest.fixture
def wrap_observer() -> type[ConcurrencyObserver]:
    """Return the ``ConcurrencyObserver`` class for wrapping a backend client."""
    return ConcurrencyObserver


@pytest.fixture
def make_echo_backend() -> type[EchoBackend]:
    """Return the ``EchoBackend`` class for building a mock backend."""
    return EchoBackend
