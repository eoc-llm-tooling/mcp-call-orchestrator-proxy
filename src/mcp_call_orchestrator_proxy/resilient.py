"""Self-healing backend connection that survives the backend coming and going.

The proxy is meant to run as an always-on ``systemctl --user`` service while the
backend is launched by the user only on demand. So the backend is
routinely *absent at startup* and comes and goes during a session.
``ResilientBackend`` wraps the real backend client behind the ``MCPBackendClient``
protocol and owns a background supervisor task that keeps a single live connection
healthy: connect -> hold open -> on break, back off and retry, forever, until
shutdown.

Design notes:

- Sits *below* the serializing orchestrator (``QueuedMCPBackend`` -> here ->
  real ``fastmcp.Client``), so it never adds concurrency of its own to the
  backend.
- Break detection is **network-silent**: it never runs a timed ping against the
  backend. A dropped connection is signalled by a delegated call failing with a
  connection-type error (the authoritative signal) and, when available, the
  underlying client's ``is_connected()``. Reconnection-after-reappear needs no
  polling either - the backoff retry loop *is* the "wait for it to come back".
- What the backend negotiated (``metadata``: protocol version, capabilities,
  instructions) is recorded on every connect and kept after the
  connection drops, read from whichever result the protocol version produced:
  the ``initialize`` handshake or ``server/discover``. The proxy answers its own
  clients' ``initialize`` and ``server/discover`` from it, never from the
  backend, so a client can always complete its handshake - "stay connectable,
  degrade".
- Shutdown is **prompt**: the backoff sleep is interruptible by the shutdown
  event, so SIGTERM during a long backoff tears down immediately rather than
  stalling systemd's stop timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import random
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, TypeVar

import anyio
import httpx2
from mcp import MCPError
from mcp import types as mcp_types
from mcp.types.version import MODERN_PROTOCOL_VERSIONS

from mcp_call_orchestrator_proxy.interfaces import (
    MCPBackendClient,
    NegotiatedBackendClient,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Exceptions that mean "the backend connection is broken", not "a tool failed".
# A failed tool returns CallToolResult(isError=True); only transport/protocol
# problems surface as exceptions here. Mirrors the set FastMCP's own proxy
# treats as upstream errors.
_CONNECTION_ERRORS: tuple[type[BaseException], ...] = (
    RuntimeError,
    TimeoutError,
    httpx2.HTTPError,
    anyio.ClosedResourceError,
    anyio.EndOfStream,
    anyio.BrokenResourceError,
)

# A backend that restarts while keeping its HTTP listener up answers any request
# still carrying the now-unknown streamable-HTTP session id with HTTP 404. The MCP
# SDK surfaces the JSON-RPC error in that response's body when there is one, and
# otherwise a stand-in of its own. Either means the session is gone - a broken
# connection to recover from, not a failed call - but neither is one of the
# transport-level exceptions in _CONNECTION_ERRORS, so they are matched exactly.
# Only a session-holding (pre-2026-07-28) HTTP connection can produce them.
_SESSION_GONE_SIGNALS: frozenset[tuple[int, str]] = frozenset(
    {
        (mcp_types.INVALID_REQUEST, "Session terminated"),  # the SDK's stand-in
        (mcp_types.INVALID_REQUEST, "Session not found"),  # Python MCP SDK server
        (-32001, "Session not found"),  # TypeScript MCP SDK server
    }
)

# The SDK's own generic "the session is gone" signal: its JSON-RPC dispatcher
# synthesizes this for every pending request once its read stream ends, whatever
# the transport. That is exactly what a stdio backend's dead subprocess produces -
# the pipe closing is swallowed *inside* the SDK's receive loop and turned into this
# per-request error, so it never reaches us as one of the raw anyio stream errors
# in _CONNECTION_ERRORS.
_CONNECTION_CLOSED_CODE = mcp_types.CONNECTION_CLOSED


def _is_connection_broken_signal(exc: BaseException) -> bool:
    """True for a "the session is gone, not just this call failed" signal - the
    SDK's generic one or a streamable-HTTP backend's clean 404.

    Deliberately narrow beyond that: an ordinary failed or unknown tool call also
    raises ``MCPError``, so matching the whole class would turn a per-call error
    into a full connection teardown.
    """
    if not isinstance(exc, MCPError):
        return False
    if exc.error.code == _CONNECTION_CLOSED_CODE:
        return True
    return (exc.error.code, exc.error.message) in _SESSION_GONE_SIGNALS


def _lists_tools_for_headers(client: NegotiatedBackendClient) -> bool:
    """Whether a new connection must list the backend's tools before serving.

    On 2026-07-28 a client sends a tool's ``x-mcp-header`` arguments as
    ``Mcp-Param-*`` HTTP headers only for tools it has listed itself, and the
    backend rejects a call without them. FastMCP's tool cache outlives a
    reconnect, so nothing else would list them on the new connection.
    """
    capabilities = client.server_capabilities
    return (
        client.protocol_version in MODERN_PROTOCOL_VERSIONS
        and capabilities is not None
        and capabilities.tools is not None
    )


@dataclass(frozen=True)
class BackendMetadata:
    """What the backend negotiated on its last successful connect.

    Read from the ``initialize`` handshake's result (protocol versions up to
    2025-11-25) or from ``server/discover`` (2026-07-28 and later); the fields
    are the ones both carry.
    """

    protocol_version: str | None
    capabilities: mcp_types.ServerCapabilities | None
    instructions: str | None


class MethodNotSupportedError(MCPError):
    """The backend answered that it does not implement the requested method.

    JSON-RPC "Method not found", e.g. ``prompts/list`` on a backend that serves
    tools only. A normal answer, not a fault. It keeps the backend's own
    ``ErrorData``, so FastMCP's proxy still reads it as ``METHOD_NOT_FOUND`` and
    serves an empty listing.
    """


class BackendUnavailableError(RuntimeError):
    """Raised when a backend operation is attempted while the backend is unreachable.

    A ``RuntimeError`` subclass so FastMCP's proxy layer maps it to a clean MCP
    error for the calling client.
    """


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class ResilientBackend:
    """Keeps a single backend connection alive across restarts of the backend.

    Implements ``MCPBackendClient`` so it slots transparently between the
    serializing ``QueuedMCPBackend`` and the real ``fastmcp.Client``. Builds each
    connection from ``factory`` (a fresh client per attempt), which sidesteps any
    stale-session subtleties on reconnect.
    """

    def __init__(
        self,
        factory: Callable[[], NegotiatedBackendClient],
        *,
        backend_url: str = "",
        connect_timeout: float = 30.0,
        initial_backoff: float = 1.0,
        max_backoff: float = 30.0,
        backoff_multiplier: float = 2.0,
        health_poll: float = 5.0,
    ) -> None:
        self._factory = factory
        self._backend_url = backend_url
        self._connect_timeout = connect_timeout
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._multiplier = backoff_multiplier
        self._health_poll = health_poll

        self._live: NegotiatedBackendClient | None = None
        self._metadata: BackendMetadata | None = None

        self._shutdown = asyncio.Event()
        self._broken = asyncio.Event()
        self._first_attempt_done = asyncio.Event()
        self._supervisor_task: asyncio.Task[None] | None = None
        self._connect_listeners: list[Callable[[], Coroutine[Any, Any, None]]] = []
        self._listener_tasks: set[asyncio.Task[None]] = set()

        # Status bookkeeping (surfaced by the backend_status tool).
        self._connected_since: datetime.datetime | None = None
        self._last_error: str | None = None
        self._consecutive_failures = 0
        self._current_backoff = 0.0

    def add_connect_listener(
        self, listener: Callable[[], Coroutine[Any, Any, None]]
    ) -> None:
        """Run ``listener`` as a task each time a connection becomes live.

        The task runs beside the held connection, so it may call the backend,
        through the proxy's queue or directly; it is cancelled on shutdown. Its
        calls carry no client request, so the queue logs them as the proxy's.
        """
        self._connect_listeners.append(listener)

    # -- lifecycle -----------------------------------------------------------

    async def __aenter__(self) -> ResilientBackend:
        """Start the supervisor and wait for the first connect attempt to resolve.

        Returns once the first attempt has either connected or failed - never
        raising. When the backend is up the connection is ready on return; when it
        is down the supervisor keeps retrying in the background and the proxy
        still starts cleanly.
        """
        self._shutdown.clear()
        self._first_attempt_done = asyncio.Event()
        self._supervisor_task = asyncio.create_task(
            self._supervise(), name="backend-supervisor"
        )
        cap = self._connect_timeout + self._initial_backoff + 5.0
        try:
            await asyncio.wait_for(self._first_attempt_done.wait(), timeout=cap)
        except TimeoutError:
            logger.warning(
                "resilient: first backend connect attempt did not resolve within "
                "%.1fs; continuing in the background",
                cap,
            )
        return self

    async def __aexit__(
        self, exc_type: object, exc_val: object, exc_tb: object
    ) -> None:
        """Stop the supervisor promptly and close the live connection."""
        self._shutdown.set()
        task = self._supervisor_task
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=self._connect_timeout + 5.0)
            except TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._supervisor_task = None
        for listener_task in list(self._listener_tasks):
            listener_task.cancel()
        await asyncio.gather(*self._listener_tasks, return_exceptions=True)
        self._live = None

    # -- supervisor ----------------------------------------------------------

    async def _supervise(self) -> None:
        """Maintain a live backend connection, reconnecting with bounded backoff."""
        delay = self._initial_backoff
        while not self._shutdown.is_set():
            try:
                client = self._factory()
                # Entering the client negotiates the protocol version with the
                # backend, by the SDK's default: server/discover first, falling
                # back to the initialize handshake.
                async with client:
                    self._metadata = BackendMetadata(
                        protocol_version=client.protocol_version,
                        capabilities=client.server_capabilities,
                        instructions=client.instructions,
                    )
                    if _lists_tools_for_headers(client):
                        await client.list_tools()
                    self._live = client
                    self._connected_since = _utcnow()
                    failures = self._consecutive_failures
                    self._consecutive_failures = 0
                    self._last_error = None
                    self._current_backoff = 0.0
                    delay = self._initial_backoff
                    if failures:
                        # A recovery is logged at the same severity as the outage
                        # it ends. Otherwise an operator filtering to warnings sees
                        # the backend go down and never sees it come back, and every
                        # transient blip reads as an unresolved incident.
                        logger.warning(
                            "resilient: backend reconnected after %d failed "
                            "attempt(s) - outage over (%s)",
                            failures,
                            self._backend_url,
                        )
                    else:
                        logger.info(
                            "resilient: backend connected (%s, protocol %s)",
                            self._backend_url,
                            client.protocol_version,
                        )
                    for listener in self._connect_listeners:
                        task = asyncio.create_task(
                            listener(), name="backend-connect-listener"
                        )
                        self._listener_tasks.add(task)
                        task.add_done_callback(self._listener_tasks.discard)
                    self._first_attempt_done.set()
                    await self._hold(client)
            except _CONNECTION_ERRORS as exc:
                self._note_failure(exc, unexpected=False)
            except Exception as exc:
                # The supervisor must never die: log loudly, then retry.
                self._note_failure(exc, unexpected=True)
            finally:
                self._live = None
                self._broken.clear()
                self._first_attempt_done.set()

            if self._shutdown.is_set():
                break
            self._current_backoff = delay
            await self._race(self._shutdown, timeout=self._jitter(delay))
            delay = min(delay * self._multiplier, self._max_backoff)

        logger.debug("resilient: supervisor exiting")

    async def _hold(self, client: MCPBackendClient) -> None:
        """Keep the connection open until it breaks or shutdown is requested.

        Waits on the ``_broken``/``_shutdown`` signals with a local timeout; the
        optional ``is_connected()`` check catches a transport that closed while
        idle. No network traffic is generated against the backend.
        """
        is_connected = getattr(client, "is_connected", None)
        while not self._shutdown.is_set() and not self._broken.is_set():
            if callable(is_connected) and not is_connected():
                break
            await self._race(self._shutdown, self._broken, timeout=self._health_poll)

    def _note_failure(self, exc: BaseException, *, unexpected: bool) -> None:
        self._consecutive_failures += 1
        self._last_error = f"{type(exc).__name__}: {exc}"
        if unexpected:
            logger.exception(
                "resilient: unexpected error in backend supervisor; will retry"
            )
        elif self._consecutive_failures == 1:
            logger.warning("resilient: backend connection failed: %s", self._last_error)
        else:
            logger.debug(
                "resilient: backend still unavailable (attempt %d): %s",
                self._consecutive_failures,
                self._last_error,
            )

    def _jitter(self, delay: float) -> float:
        """Apply +/-20% jitter so repeated retries do not lock-step."""
        return delay * (0.8 + 0.4 * random.random())

    async def _race(self, *events: asyncio.Event, timeout: float) -> None:
        """Return when any event is set or ``timeout`` elapses (whichever first)."""
        waiters = [asyncio.ensure_future(e.wait()) for e in events]
        try:
            await asyncio.wait(
                waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for w in waiters:
                w.cancel()
            for w in waiters:
                with contextlib.suppress(asyncio.CancelledError):
                    await w

    # -- status --------------------------------------------------------------

    def status_snapshot(self) -> dict[str, Any]:
        """Structured backend-health snapshot for the ``backend_status`` tool."""
        connected = self._live is not None
        return {
            "connected": connected,
            "backend_url": self._backend_url,
            "last_connected_at": (
                self._connected_since.isoformat()
                if self._connected_since is not None
                else None
            ),
            "last_error": None if connected else self._last_error,
            "consecutive_failures": self._consecutive_failures,
            "current_backoff_seconds": self._current_backoff,
        }

    def _unavailable_message(self) -> str:
        base = "Backend is not available"
        return f"{base} at {self._backend_url}" if self._backend_url else base

    def _require_live(self) -> NegotiatedBackendClient:
        client = self._live
        if client is None:
            raise BackendUnavailableError(self._unavailable_message())
        return client

    def _connection_failed(self, exc: BaseException) -> BackendUnavailableError:
        self._broken.set()
        self._last_error = f"{type(exc).__name__}: {exc}"
        return BackendUnavailableError(f"{self._unavailable_message()}: {exc}")

    @property
    def metadata(self) -> BackendMetadata | None:
        """What the backend negotiated on its last successful connect.

        Kept after the connection drops; ``None`` until the first connect. Local
        state only: reading it never touches the backend.
        """
        return self._metadata

    # -- delegated MCPBackendClient surface ----------------------------------

    @property
    def protocol_version(self) -> str | None:
        """``None``: see ``MCPBackendClient.protocol_version``."""
        return None

    async def _guarded(self, op: Awaitable[_T]) -> _T:
        """Run a delegated backend call, turning a broken connection into a reconnect.

        A transport-level error (``_CONNECTION_ERRORS``) or a connection-broken
        ``MCPError`` signal (``_is_connection_broken_signal``) marks the connection
        broken - which wakes the supervisor to tear down and rebuild the client -
        and is re-raised as ``BackendUnavailableError``. A "Method not found"
        answer is re-raised as ``MethodNotSupportedError``. Any other error is an
        ordinary per-call failure and propagates unchanged.
        """
        try:
            return await op
        except _CONNECTION_ERRORS as exc:
            raise self._connection_failed(exc) from exc
        except MCPError as exc:
            if _is_connection_broken_signal(exc):
                raise self._connection_failed(exc) from exc
            if exc.error.code == mcp_types.METHOD_NOT_FOUND:
                raise MethodNotSupportedError.from_error_data(exc.error) from exc
            raise

    async def list_tools(self) -> list[mcp_types.Tool]:
        client = self._require_live()
        return await self._guarded(client.list_tools())

    async def call_tool_mcp(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        meta: dict[str, Any] | None = None,
    ) -> mcp_types.CallToolResult:
        client = self._require_live()
        return await self._guarded(client.call_tool_mcp(name, arguments, meta=meta))

    async def list_resources(self) -> list[mcp_types.Resource]:
        client = self._require_live()
        return await self._guarded(client.list_resources())

    async def list_resource_templates(self) -> list[mcp_types.ResourceTemplate]:
        client = self._require_live()
        return await self._guarded(client.list_resource_templates())

    async def read_resource_mcp(
        self, uri: str, meta: dict[str, Any] | None = None
    ) -> mcp_types.ReadResourceResult:
        client = self._require_live()
        return await self._guarded(client.read_resource_mcp(uri, meta=meta))

    async def list_prompts(self) -> list[mcp_types.Prompt]:
        client = self._require_live()
        return await self._guarded(client.list_prompts())

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        meta: dict[str, Any] | None = None,
    ) -> mcp_types.GetPromptResult:
        client = self._require_live()
        return await self._guarded(client.get_prompt(name, arguments, meta=meta))
