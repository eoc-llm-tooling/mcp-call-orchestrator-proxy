# mcp-call-orchestrator-proxy

A resilient, call-serializing **MCP proxy** that sits in front of a single
backend MCP server as its only client and makes it safe for multiple concurrent
clients.

Some MCP servers have session-routing bugs or no call serialization, so two
concurrent MCP clients (e.g. two agents, or an agent plus a manual test) can
hang or step on each other. This proxy is the backend's only client: it ends
each client's requests at itself, serializes the calls it forwards to the one
backend, and exposes the backend's
**exact same tools with exact schema parity** — so clients see a well-behaved
server, unchanged except for the added stability.

## Features

- **Exact tool parity** — mirrors the backend's tools with identical names and
  schemas; nothing to keep in sync by hand.
- **Multi-client safe** — independent MCP clients can connect over HTTP at the
  same time without cross-talk or hangs.
- **Current MCP protocol versions, both kinds** — clients on the session-based
  versions (up to 2025-11-25, opening with `initialize`) and on the sessionless
  2026-07-28 version are served side by side, and the backend's version is
  negotiated automatically, so any client reaches a backend of either kind.
- **The backend's guidance is passed on** — clients receive the backend's server
  `instructions`, and are offered only the capabilities the backend has (tools
  always, for `backend_status`).
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
- **Description overrides** — an optional file replaces, prepends to or appends
  to the descriptions of the backend's tools, arguments, prompts and resources,
  and its server instructions. Names and schemas stay the backend's own. See
  [Description overrides](#description-overrides).

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- A backend MCP server to front. 
- Linux with a user-level `systemd`, or `supervisord`, if you want the proxy to
  run as a background service (optional; see [Run as a service](#run-as-a-service)).

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
backend, and any other count stops it at startup with an error. The `auth` string is sent as a Bearer token. TLS verification is *not*
part of the `mcpServers` format, so it stays a separate knob
(`MCP_PROXY_BACKEND_VERIFY_TLS`, default `false`). An example lives at
[`deploy/systemd/backend.example.json`](deploy/systemd/backend.example.json).

FastMCP's own extensions to the entry are covered in
[FastMCP config extensions](#fastmcp-config-extensions): `tools` is honoured in
part, and the tag keys are refused.

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
| `MCP_PROXY_SERVER_NAME` | `mcp-call-orchestrator-proxy` | Name the proxy reports as its MCP server identity (the `initialize` handshake, or `server/discover`). Some MCP clients (e.g. VS Code/Copilot's tool picker) label/dedupe servers by this rather than by the key you gave the entry in your own client config - set a unique value per instance when running more than one, or they'll look identical to those clients. |
| `MCP_PROXY_LISTEN_HOST` | `127.0.0.1` | Host the proxy itself binds to. |
| `MCP_PROXY_LISTEN_PORT` | `27125` | Port the proxy itself listens on. |
| `MCP_PROXY_MAX_CONCURRENT_CALLS` | `1` | Concurrent calls allowed to the backend (`1` = fully serialized). |
| `MCP_PROXY_CALL_TIMEOUT_SECONDS` | `120` | Timeout, applied separately, for a call's wait in the queue and for the backend call itself, each from its own start. A call that exceeds either fails alone, with an error that says which timed out and after how long; the calls queued behind it still run. A call that times out in the queue, or whose client cancels it there, never reaches the backend. |
| `MCP_PROXY_CONNECT_TIMEOUT_SECONDS` | `30` | Timeout for a single backend connect attempt, protocol negotiation included. |
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
| `MCP_PROXY_OVERRIDES` | *(unset)* | Path to a JSON file overriding the backend's descriptions and instructions. See [Description overrides](#description-overrides). |

The most commonly tuned values are also available as CLI flags
(`--host`, `--port`, `--transport`, `--backend-config`, `--overrides`, `--connect-timeout`,
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
- **The backend config can disable a tool** — `"enabled": false` in its
  `tools` key hides the tool whatever these settings say; see
  [FastMCP config extensions](#fastmcp-config-extensions).
- **Unknown filter names** — after the first successful discovery for a
  surface, each name that does not exist on the backend is reported once per
  process as a `WARNING` (typos / stale lists). Startup is never blocked by a
  misconfigured filter.
- Resource lists cover **concrete resources and resource templates** with the
  same name set (a template that could synthesize a hidden URI would otherwise
  bypass a resource allow-list).

### FastMCP config extensions

A backend entry written for FastMCP may carry a `tools` key. The proxy applies
the parts of it that change only what agents are told, or what they can call:

```json
{
  "mcpServers": {
    "some-mcp-server": {
      "command": "uv",
      "args": ["run", "some-mcp-server"],
      "tools": {
        "search": {
          "description": "Search the notes by keyword.",
          "arguments": {"query": {"description": "Plain words."}}
        },
        "delete_note": {"enabled": false}
      }
    }
  }
}
```

- **`description`** of a tool, and of each of its `arguments`, replaces the
  backend's text, like a `replace` in the
  [overrides file](#description-overrides), and a name the backend does not
  have is reported in the same way. Where the overrides file sets the same text,
  the file's entry applies and a `WARNING` names the text at startup.
- **`enabled: false`** hides the tool and makes it uncallable, as a
  `MCP_PROXY_TOOL_DENY` entry does. It wins over `MCP_PROXY_TOOL_ALLOW`: a tool
  both name stays hidden, with a `WARNING` at startup.
- **Every other field** stops the proxy at startup with an error naming the
  field and why: renaming a tool or an argument, hiding, defaulting or
  requiring an argument, `examples`, `title`, `version`, `tags` and `meta`, and
  an entry for `backend_status`.
- At startup an `INFO` record counts what the key applied.

`include_tags` and `exclude_tags` are not supported and stop the proxy at
startup; use exposure control to choose which tools are exposed.

The `tools` key never changes how the proxy reaches the backend: the stdio
process and TLS settings are the same with it as without it.

### Description overrides

A backend's descriptions are written for everyone who uses it. To tell your
agents something more specific, point `MCP_PROXY_OVERRIDES` (or `--overrides`)
at a JSON file that changes them:

```json
{
  "instructions": {"append": "Search before you create a note."},
  "tools": {
    "search": {
      "description": {"prepend": "Prefer this over listing every note."},
      "arguments": {
        "query": {"append": "Plain words; no search operators."}
      }
    }
  },
  "prompts": {
    "summarize": {"description": {"replace": "Summarize in three bullet points."}}
  },
  "resources": {
    "notes://index": {"description": {"append": "Updated hourly."}}
  }
}
```

Rules:

- **Text only** — names and schemas reach clients as the backend has them;
  only the descriptions named in the file, and the instructions, change.
- **Three operations** — `replace` sets the text; `prepend` and `append` (one
  or both) add the operator's text before or after the backend's, separated by
  a blank line. Where the backend has no text, the operator's stands alone.
- **Keys are the backend's own names** — tool and prompt names, argument names,
  a resource's URI or a template's `uriTemplate` (resources and templates share
  the `resources` section, as in exposure control).
- **Instructions** apply once the backend has connected: until the proxy has
  seen the backend's own instructions, clients get none, as without a file.
- **A file the proxy cannot apply stops it at startup** with an error naming
  the file and the path of each fault: one that cannot be read or parsed, a
  key given twice in one object, an unknown key, an entry or operation that
  sets nothing, `replace` combined with another operation, a `prepend` or
  `append` with empty text (`"replace": ""` blanks a text), or an entry for
  `backend_status`, which is the proxy's own tool.
- **Unknown names** — when the backend first connects, the proxy lists
  everything the backend serves, as its own calls (logged as `proxy`), and
  reports each tool, prompt, resource or argument in the file that the backend
  does not have, once, as a `WARNING`. At startup an `INFO` record names the
  file, its SHA-256 and what it overrides.
- **Prompt results** carry the same description as the prompt's listing; the
  rest of every result is relayed as the backend sends it.

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

Where systemd user services are unavailable (a container, a host without a user
session manager), a per-user `supervisord` runs it instead, with one program file
per service; templates are in [`deploy/supervisor/`](deploy/supervisor/), covered
in the same guide.

## Logs

`MCP_PROXY_LOG_LEVEL` (or `--log-level`) controls verbosity: `DEBUG`, `INFO` (default),
`WARNING`, `ERROR`, or `CRITICAL`. `WARNING`+ is backend outages and recoveries, plus what
only the operator can fix: a misconfigured exposure filter or overrides file, a call the
backend never answered; `INFO` adds a record per call as it's queued and as it completes;
`DEBUG` adds internal detail for fault-finding. A 2026-07-28 client's `Mcp-Param-*` headers are checked against the
tool list the proxy last served, so that check adds neither a record nor a backend call. A tool
call, resource read or prompt request is looked up in what the proxy last listed to its
clients, however long ago, so it adds only its own records; only a request made before any
client has listed that kind of item since the proxy started also lists the backend.

Each per-call record names the client by the name it gives itself (`clientInfo`):
`<name>-<nnn>` for a client on a session-based protocol version, one number per
session; plain `<name>` for a client on 2026-07-28, which has no session, so two
instances of one client share it; `client` for a client that gives no name; and
`proxy` for a call the proxy makes on its own.

Records go to stdout/stderr, so under systemd they land in the journal:

```bash
journalctl --user -u mcp-call-orchestrator-proxy -p warning   # outages, recoveries, operator action
journalctl --user -u mcp-call-orchestrator-proxy -f           # follow at the configured level
```

Under supervisord they go to `~/.local/state/supervisor/mcp-call-orchestrator-proxy.log`.

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
- [`docs/deployment.md`](docs/deployment.md) — running the proxy as a systemd user service or under supervisord.
- [`docs/testing.md`](docs/testing.md) — the test tiers and how to add to them.
- [`docs/coding-standards.md`](docs/coding-standards.md) — code conventions.
- [`docs/needs.md`](docs/needs.md) — what the proxy must do, and the limits on how it is built.
- [`docs/roadmap.md`](docs/roadmap.md) — what's done and what's next.
- [`docs/issues.md`](docs/issues.md) — observed defects, each with its root cause
  and fix.

## License

MIT — see [LICENSE](LICENSE).
