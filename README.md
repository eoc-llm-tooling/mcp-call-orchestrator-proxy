# mcp-call-orchestrator-proxy

A resilient, session-terminating, call-serializing **MCP proxy** that sits in
front of a single backend MCP server and makes it safe for multiple concurrent
clients.

Some MCP servers have session-routing bugs or no call serialization, so two
concurrent MCP clients (e.g. two agents, or an agent plus a manual test) can
hang or step on each other. This proxy terminates each client's MCP session,
serializes the calls it forwards to the one backend, and exposes the backend's
**exact same tools with exact schema parity** — so clients see a well-behaved
server, unchanged except for the added stability.

## Features

- **Exact tool parity** — mirrors the backend's tools with identical names and
  schemas; nothing to keep in sync by hand.
- **Multi-session safe** — independent MCP clients can connect over HTTP at the
  same time without cross-talk or hangs.
- **Call serialization** — calls to the backend are queued and orchestrated
  (configurable concurrency) instead of racing each other.
- **Resilient to the backend coming and going** — starts cleanly whether or not
  the backend is running yet, reconnects automatically (gentle, bounded
  backoff — never hammers a down endpoint) after the backend starts, restarts,
  or drops mid-session, and shuts down promptly on `SIGTERM` even mid-backoff.
- **`backend_status` tool** — lets a client ask "is the backend reachable right
  now?" without triggering a failing call. Returns `connected`, `backend_url`,
  `last_connected_at`, `last_error`, `consecutive_failures`, and
  `current_backoff_seconds`.
- **Exposure control** — optional exact-name allow-lists or deny-lists for tools,
  resources (including resource templates) and prompts. Hidden items are absent
  from listings and uncallable by name. Off by default (full parity). See
  [Exposure control](#exposure-control).

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- A backend MCP server to front. 
- Linux with a user-level `systemd` if you want the proxy to run as a background
  service (optional; see [Run as a service](#run-as-a-service)).

## Installation

From the root of this repository:

```bash
uv sync
```

This creates `.venv/` with all dependencies and installs the
`mcp-call-orchestrator-proxy` console script into it. Verify it worked:

```bash
uv run mcp-call-orchestrator-proxy --help
```

## Configuration

Settings load from environment variables (or a `.env` file in the current
working directory) via `pydantic-settings`, all prefixed `MCP_PROXY_`. There are
two ways to point the proxy at its backend.

### Option 1 (recommended): a standard `mcpServers` JSON config file

Point the proxy at a JSON file in the same `mcpServers` format Claude Desktop /
Cursor / Copilot use. FastMCP parses and validates it, so there's no schema to
hand-write:

```json
{
  "mcpServers": {
    "some-mcp-server": {
      "url": "https://127.0.0.1:27124/mcp/",
      "transport": "http",
      "auth": "YOUR_BEARER_TOKEN"
    }
  }
}
```

```bash
export MCP_PROXY_BACKEND_CONFIG="/path/to/backend.json"
```

The config **must define exactly one server** — this proxy fronts a single
backend. The `auth` string is sent as a Bearer token. TLS verification is *not*
part of the `mcpServers` format, so it stays a separate knob
(`MCP_PROXY_BACKEND_VERIFY_TLS`, default `false`). An example lives at
[`deploy/systemd/backend.example.json`](deploy/systemd/backend.example.json).

The backend can just as well be a **stdio server** — most of the MCP ecosystem
ships this way — described the same way any MCP client would describe it:
command, arguments, and (optionally) environment and working directory:

```json
{
  "mcpServers": {
    "some-mcp-server": {
      "command": "uv",
      "args": ["run", "some-mcp-server"],
      "env": { "SOME_VAR": "value" }
    }
  }
}
```

The proxy owns that process's whole life: it starts the backend on connect,
keeps the same process across reconnects, and kills it on disconnect or
shutdown — nothing is left running behind. (A `keep_alive` field in this JSON
is accepted by the format but has no effect here; the proxy always manages the
process itself.) This is independent of the *client*-facing transport covered
next — a proxy listening over HTTP can front a stdio backend, and vice versa.

### Option 2: individual environment variables

If `MCP_PROXY_BACKEND_CONFIG` is unset, the proxy builds the connection from
these instead. `MCP_PROXY_BACKEND_API_KEY` is required in this mode.

| Variable | Default | Meaning |
|---|---|---|
| `MCP_PROXY_BACKEND_CONFIG` | *(unset)* | Path to an `mcpServers` JSON file (Option 1). Wins over the two fields below. |
| `MCP_PROXY_BACKEND_API_KEY` | *(required in Option 2)* | Bearer token for the backend. |
| `MCP_PROXY_BACKEND_MCP_URL` | `https://127.0.0.1:27124/mcp/` | Backend's Streamable HTTP MCP endpoint. |
| `MCP_PROXY_BACKEND_VERIFY_TLS` | `false` | Verify the backend's TLS cert. `false` is normal for a self-signed cert. |
| `MCP_PROXY_SERVER_NAME` | `mcp-call-orchestrator-proxy` | Name the proxy reports as its MCP server identity (the `initialize` handshake). Some MCP clients (e.g. VS Code/Copilot's tool picker) label/dedupe servers by this rather than by the key you gave the entry in your own client config - set a unique value per instance when running more than one, or they'll look identical to those clients. |
| `MCP_PROXY_LISTEN_HOST` | `127.0.0.1` | Host the proxy itself binds to. |
| `MCP_PROXY_LISTEN_PORT` | `27125` | Port the proxy itself listens on. |
| `MCP_PROXY_MAX_CONCURRENT_CALLS` | `1` | Concurrent calls allowed to the backend (`1` = fully serialized). |
| `MCP_PROXY_CALL_TIMEOUT_SECONDS` | `120` | Timeout for a single backend tool call. |
| `MCP_PROXY_CONNECT_TIMEOUT_SECONDS` | `30` | Timeout for a single backend connect/initialize attempt. |
| `MCP_PROXY_RECONNECT_INITIAL_BACKOFF_SECONDS` | `1` | First delay before retrying a failed connection. |
| `MCP_PROXY_RECONNECT_MAX_BACKOFF_SECONDS` | `30` | Cap on the exponential reconnect backoff. |
| `MCP_PROXY_RECONNECT_BACKOFF_MULTIPLIER` | `2` | Growth factor applied to the backoff after each failure. |
| `MCP_PROXY_BACKEND_HEALTH_POLL_SECONDS` | `5` | How often a held connection is locally re-checked (no network traffic). |
| `MCP_PROXY_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. See [Logs](#logs). |
| `MCP_PROXY_TOOL_ALLOW` | *(unset)* | Comma-separated tool names to expose (exact match). When set, only these tools are visible; `MCP_PROXY_TOOL_DENY` is ignored for tools. |
| `MCP_PROXY_TOOL_DENY` | *(unset)* | Comma-separated tool names to hide (exact match). Ignored when `MCP_PROXY_TOOL_ALLOW` is set. |
| `MCP_PROXY_RESOURCE_ALLOW` | *(unset)* | Comma-separated resource URIs / template `uriTemplate`s to expose (exact match). When set, `MCP_PROXY_RESOURCE_DENY` is ignored. |
| `MCP_PROXY_RESOURCE_DENY` | *(unset)* | Comma-separated resource URIs / template `uriTemplate`s to hide. Ignored when allow is set. |
| `MCP_PROXY_PROMPT_ALLOW` | *(unset)* | Comma-separated prompt names to expose (exact match). When set, `MCP_PROXY_PROMPT_DENY` is ignored. |
| `MCP_PROXY_PROMPT_DENY` | *(unset)* | Comma-separated prompt names to hide. Ignored when allow is set. |

The most commonly tuned values are also available as CLI flags
(`--host`, `--port`, `--transport`, `--backend-config`, `--connect-timeout`,
`--reconnect-initial-backoff`, `--reconnect-max-backoff`, `--log-level`) — run
`uv run mcp-call-orchestrator-proxy --help` for the full list. CLI flags win over
environment variables.

### Exposure control

By default the proxy exposes the backend's full tool / resource / prompt surface
(plus `backend_status`). When an agent should only see a subset, set an
allow-list or deny-list per surface:

```bash
# Only these tools (and nothing else — include backend_status if you want it):
export MCP_PROXY_TOOL_ALLOW="echo,slow_echo,backend_status"

# Or hide specific tools while leaving the rest visible:
export MCP_PROXY_TOOL_DENY="hang,crash"
```

Rules:

- **Exact names only** — no globs or regex. Names match the backend's real
  tool/prompt `name`, a resource's URI string, or a template's `uriTemplate`.
- **Blank means unset** — `MCP_PROXY_TOOL_ALLOW=` is the same as leaving the
  variable out (not "allow nothing").
- **Allow beats deny** on the same surface — if both are set, the allow-list
  decides alone and a `WARNING` names that surface at startup.
- **Hidden is uncallable** — filtered items are absent from listings and
  return a protocol-level "not found" if invoked by name.
- **`backend_status` is not special** — it is filtered like any other tool. If
  a tool allow-list omits it (or a deny-list names it), a `WARNING` is logged
  once at startup; the filter is still honoured.
- **Unknown filter names** — after the first successful discovery for a
  surface, each name that does not exist on the backend is reported once per
  process as a `WARNING` (typos / stale lists). Startup is never blocked by a
  misconfigured filter.
- Resource lists cover **concrete resources and resource templates** with the
  same name set (a template that could synthesize a hidden URI would otherwise
  bypass a resource allow-list).

## Usage

### Run it manually

```bash
# Option 1 — JSON backend config:
export MCP_PROXY_BACKEND_CONFIG="/path/to/backend.json"
uv run mcp-call-orchestrator-proxy run

# Option 2 — individual env vars:
export MCP_PROXY_BACKEND_API_KEY="your-real-api-key"
export MCP_PROXY_BACKEND_MCP_URL="https://127.0.0.1:27124/mcp/"   # only if not the default
uv run mcp-call-orchestrator-proxy run
```

By default it serves Streamable HTTP on `http://127.0.0.1:27125/mcp/`. Point your
MCP client(s) there instead of at the backend directly. It's safe to start this
before the backend is running — it will sit connectable and reconnect
automatically once the backend appears (see `backend_status`).

For `stdio` transport instead (e.g. for a client that spawns the process
itself): `uv run mcp-call-orchestrator-proxy run --transport stdio`.

### Check backend health

Any connected client can call the `backend_status` tool to check whether the
backend is currently reachable, instead of guessing from a failed call:

```json
{"connected": true, "backend_url": "https://127.0.0.1:27124/mcp/",
 "last_connected_at": "2026-07-11T18:02:03+00:00", "last_error": null,
 "consecutive_failures": 0, "current_backoff_seconds": 1.0}
```

## Run as a service

The intended deployment is a `systemd --user` service that starts at login (or at
boot, with lingering enabled) and stays up whether or not the backend is running.
Unit templates are in [`deploy/systemd/`](deploy/systemd/), and
[`docs/deployment.md`](docs/deployment.md) walks through installing them.

## Logs

`MCP_PROXY_LOG_LEVEL` (or `--log-level`) controls verbosity: `DEBUG`, `INFO` (default),
`WARNING`, `ERROR`, or `CRITICAL`. `WARNING`+ is just backend outages and recoveries;
`INFO` adds a record per call as it's queued and as it completes; `DEBUG` adds internal
detail for fault-finding.

Records go to stdout/stderr, so under systemd they land in the journal:

```bash
journalctl --user -u mcp-call-orchestrator-proxy -p warning   # outages/recoveries only
journalctl --user -u mcp-call-orchestrator-proxy -f           # follow at the configured level
```

## Development

```bash
uv run poe gate         # lint + format-check + mypy + unit + component + integration*
uv run poe format       # auto-format
uv run poe test-int     # integration tests only, as a standalone shortcut
uv run poe check-py313  # optional: full gate on Python 3.13, own venv (forward-compat only; floor stays 3.12)
```

\* The integration tier runs against your own backend, read from
`tests/integration/backend.json` (a gitignored `mcpServers` file — copy
`tests/integration/backend.example.json`). Without it, that step skips itself
cleanly and says why; the gate stays green. Everything else is offline: no
network and no backend of your own is needed.

## Documentation

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — canonical technical overview:
  components, call flow, invariants, resilience model.
- [`docs/deployment.md`](docs/deployment.md) — running the proxy as a systemd user service.
- [`docs/testing.md`](docs/testing.md) — the test tiers and how to add to them.
- [`docs/coding-standards.md`](docs/coding-standards.md) — code conventions.
- [`docs/roadmap.md`](docs/roadmap.md) — what's done and what's next.
- [`docs/issues.md`](docs/issues.md) — observed defects, each with its root cause
  and fix.

## License

MIT — see [LICENSE](LICENSE).
