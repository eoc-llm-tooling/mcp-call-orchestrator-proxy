"""A real MCP server the offline tests spawn as a child process.

The mock backends in ``tests/conftest.py`` are in-process objects that satisfy
the ``MCPBackendClient`` Protocol - no protocol, no transport, no process. They
can only prove the proxy agrees with *our own* idea of MCP. This module is the
other half: the tools are ours, but the protocol and transport handling under
them are FastMCP's, so a protocol- or transport-level mistake in the proxy has
something real to fail against.

The tools exist because the scenarios need them, and only a server we control
provides them: a *slow* tool (widens the window in which calls from different
sessions overlap at the backend), a *hanging* tool (a call that never returns),
and ``call_stats`` - the backend's own account of how many tool calls it has
served and how many ever ran at once. That last one is the point: concurrency is
measured **inside the backend**, not by a wrapper in the proxy's own process, so
the serialization claim is not graded by our own homework.

Three more tools exist only for the stdio-backend scenarios, where there is no
port to identify or kill a backend by from outside: ``pid`` (this process's own
pid, so a test can tell whether a reconnect actually replaced the process),
``crash`` (exits the process outright, modelling a stdio backend dying), and
``env_var`` (echoes back one entry from this process's own environment, so a
test can see exactly what reached the child).

Run as a script; it either binds the loopback port it is given and serves
Streamable HTTP on it, or serves over stdio::

    python tests/support/probe_backend_server.py --port 12345
    python tests/support/probe_backend_server.py --stdio
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import socket
from collections.abc import AsyncIterator

from fastmcp import FastMCP

# A hang must outlive any timeout under test without pinning the process open
# forever if a teardown is ever missed.
_HANG_SECONDS = 3600.0


class _CallStats:
    """The backend's own record of the tool calls it served.

    Exact without locking: FastMCP runs async tools on the server's single event
    loop (one thread), and no ``await`` separates the counter updates below, so
    they cannot interleave.
    """

    def __init__(self) -> None:
        self.active = 0
        self.peak_active = 0
        self.total_calls = 0

    @contextlib.asynccontextmanager
    async def in_flight(self) -> AsyncIterator[None]:
        """Count one tool call for as long as it is running."""
        self.active += 1
        self.total_calls += 1
        self.peak_active = max(self.peak_active, self.active)
        try:
            yield
        finally:
            self.active -= 1


def build_server() -> FastMCP:
    """Build the probe backend. Performs no I/O."""
    stats = _CallStats()
    mcp: FastMCP = FastMCP(name="probe-backend")

    @mcp.tool
    async def echo(value: str) -> str:
        """Echo the given value back verbatim."""
        async with stats.in_flight():
            return value

    @mcp.tool
    async def slow_echo(value: str, delay_seconds: float = 0.2) -> str:
        """Echo the given value back verbatim, after a delay."""
        async with stats.in_flight():
            await asyncio.sleep(delay_seconds)
            return value

    @mcp.tool
    async def hang() -> str:
        """Never answer. Models a backend call that has gone away."""
        async with stats.in_flight():
            await asyncio.sleep(_HANG_SECONDS)
            return "unreachable"

    @mcp.tool
    async def call_stats() -> dict[str, int]:
        """Report how many tool calls this backend served, and the most it ever
        ran at once. Deliberately not counted in its own figures."""
        return {
            "active": stats.active,
            "peak_active": stats.peak_active,
            "total_calls": stats.total_calls,
        }

    @mcp.tool
    async def pid() -> int:
        """Report this process's own pid."""
        return os.getpid()

    @mcp.tool
    async def crash() -> None:
        """Exit the process immediately. Models a stdio backend dying."""
        os._exit(1)

    @mcp.tool
    async def env_var(name: str) -> str | None:
        """Report the value of one environment variable in this process."""
        return os.environ.get(name)

    return mcp


def _serve_http(port: int) -> None:
    """Bind the given loopback port and serve Streamable HTTP on it until killed."""
    # Bind here rather than letting uvicorn do it, so that a port already taken
    # fails loudly and immediately instead of racing the parent's readiness poll.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))

    asyncio.run(
        build_server().run_http_async(
            host="127.0.0.1",
            port=port,
            sockets=[sock],
            transport="http",
            show_banner=False,
            log_level="warning",
        )
    )


def _serve_stdio() -> None:
    """Serve over stdio until the parent closes the pipes (or `crash` is called)."""
    asyncio.run(build_server().run_stdio_async(show_banner=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--stdio", action="store_true")
    args = parser.parse_args()

    if args.stdio == (args.port is not None):
        parser.error("specify exactly one of --port or --stdio")

    if args.stdio:
        _serve_stdio()
    else:
        assert args.port is not None
        _serve_http(args.port)


if __name__ == "__main__":
    main()
