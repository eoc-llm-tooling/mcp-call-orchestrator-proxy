# Testing

Test approach: what each tier covers and by what mechanism. Commands live in the
[`README.md`](../README.md) `## Development` section.

## Tiers

| Tier | Directory | Backend | Transport |
|---|---|---|---|
| Unit | `tests/unit` | in-process fakes | none (direct calls) |
| Component | `tests/component` | in-process mock **or** spawned real MCP server | real loopback HTTP |
| Integration | `tests/integration` | the developer's own, from `backend.json` | real, per that config |

Unit and component are offline: no network, no configured backend. Both run in the gate
unconditionally. Integration runs in the gate when `tests/integration/backend.json` exists
(gitignored; copy `backend.example.json`) and skips itself otherwise.

### Unit

Each module against fakes, through its own API. Covers per-module logic: settings validation,
queue ordering and timeout, reconnect/backoff state machine, client construction, proxy wiring.

### Component

The whole proxy (`create_proxy_app`) served over a real loopback HTTP port by
`serve_over_http`, driven by real `fastmcp.Client` sessions.

Required for multi-session and shared-backend serialization claims: the in-memory
`Client(mcp)` transport enters a fresh lifespan per client connection, giving each client its
own orchestrator and backend connection, so it cannot represent several sessions sharing one
backend. One HTTP server — one lifespan, one orchestrator, one shared backend — can.

### Integration

The same guarantees against a real backend, backend-neutral: tools are discovered at runtime
(`probe_tool` fixture), never named in the test, so the suite works against any MCP server
described by `backend.json`.

## Component backends

**In-process mocks** (`EchoBackend`, `GatedEchoBackend`, `ConcurrencyObserver`) — objects
satisfying the `MCPBackendClient` Protocol, injected via `create_proxy_app`'s `backend_client`
/ `backend_factory` parameters. Fast, and can produce states a healthy server will not (a
connection that always fails). They implement no protocol and cross no transport.

**Spawned real MCP server** (`tests/support/probe_backend_server.py` via `spawn_backend`) —
FastMCP's server implementation with test-specific tools, run as a child process on a fixed
loopback port. Properties the mocks do not have:

- Real MCP protocol and HTTP transport between proxy and backend.
- The proxy is configured from a real `mcpServers` JSON file (`backend_config_file`), so
  `client.py` (config parsing, transport construction, `fastmcp.Client`) is exercised rather
  than bypassed.
- Concurrency is measured in the backend process (`call_stats`), not by a proxy-side wrapper.
- Backend death is real: `kill()` sends `SIGKILL`; `start()` restores the server on the same
  port.

**Spawned stdio backend** (same `probe_backend_server.py`, run with `--stdio`, via
`stdio_backend_config_file`) — the same server and tools reached over a command/args entry
instead of a URL, exercising the same `client.py` path for the stdio branch
(`tests/component/test_real_backend_stdio.py`). No `spawn_backend`-equivalent lifecycle fixture
exists for it: for a stdio backend the *proxy itself* spawns and kills the child process, which
is exactly the thing under test, so the fixture only needs to write a config, not manage a
process. A backend "dying" is modelled by the `crash` tool (`os._exit(1)`, since there is no
port to `SIGKILL` from outside); "no orphaned child remains" is checked by recording the child's
own `pid()` and polling `os.kill(pid, 0)` for `ProcessLookupError` after the proxy lets go of it.

Selection rule: use the spawned server for claims about protocol, transport, the `client.py`
path, or what the backend actually observed; use a mock for backend states that must be forced.

## Harness (`tests/conftest.py`)

Exposed as fixtures so any tier can use them without cross-directory imports.

| Fixture | Provides |
|---|---|
| `serve_over_http` | Async CM serving a `FastMCP` app on an ephemeral loopback port; one lifespan for all sessions. |
| `spawn_backend` | Factory → `SpawnedBackend` (`.url`, `.start()`, `.kill()`, `.output()`); killed at teardown. |
| `backend_config_file` | Factory writing an `mcpServers` JSON for a given URL; returns its path. |
| `stdio_backend_config_file` | Factory writing an `mcpServers` JSON with a `command`/`args`/`env` entry for the stdio probe backend; returns its path. |
| `make_echo_backend` | `EchoBackend` — one `echo` tool, verbatim, optional delay. |
| `wrap_observer` | `ConcurrencyObserver` — wraps a backend client, records peak in-flight calls. |

Probe server tools: `echo`, `slow_echo` (delay, to widen the overlap window), `hang` (never
returns), `call_stats` (total calls and peak concurrent calls, excluding itself), `pid` (this
process's own pid — a stdio backend has no port to identify it by), `crash` (`os._exit(1)`,
models a stdio backend dying), `env_var` (echoes back one entry of this process's own
environment, for the env-isolation claim). Extend it when a scenario needs backend behaviour
that only a controlled server can produce.

## Conventions

- Layout mirrors the source under `tests/{unit,component,integration}`.
- `asyncio_mode = "auto"`; no explicit asyncio marker.
- Component tests set `pytestmark = pytest.mark.component`.
- A "never happens" assertion needs a counterpart showing the observation can register the
  negative case — e.g. `test_backend_is_seen_running_calls_concurrently_when_allowed` shows
  `call_stats` reading above 1 when `max_concurrent_calls` permits it, which is what gives
  `peak_active == 1` its meaning elsewhere.

## Constraints

- **Offline tiers must not require network or a configured backend.** The spawned backend is a
  loopback child process and satisfies this (checked by running `tests/component` in a network
  namespace with only `lo` up).
- **Query the spawned backend from within the serving block.** `serve_proxy_over_http` stops
  the proxy by cancelling its uvicorn task; a new outbound async connection opened on that
  event loop afterwards fails with `httpx.ConnectError` even though the target is reachable.
  Test-harness artifact, not proxy behaviour.
