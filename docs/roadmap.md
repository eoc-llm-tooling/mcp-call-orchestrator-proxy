# Roadmap

Where the proxy is today, and what it is meant to become. Ordered by intent, not by date —
nothing here is a scheduled commitment, and the order changes when a real need does.

## Where it is today

- **Exact parity** with the backend's tools, resources and prompts — real names, original
  schemas with their `$ref`s and `$defs` as the backend wrote them, nothing hand-synced.
- **Calls are serialized** through one FIFO queue with a configurable limit (default `1`). One
  timeout bounds a call's wait in the queue and its backend call, each from its own start, so a
  single hung call can't wedge the rest, and a call whose client gave up before it started never
  reaches the backend.
- **Results are relayed as the backend sent them**, a result that does not match the tool's own
  output schema included; the clients check what they receive.
- **Multi-client safe** — independent clients connect over HTTP at once without cross-talk;
  the proxy is the backend's only client.
- **Both kinds of MCP protocol version** — clients on the session-based versions (up to
  2025-11-25) and on the sessionless 2026-07-28 are served side by side, and the backend's
  version is negotiated automatically, in any pairing. The proxy answers its clients'
  handshakes itself, passing on the backend's `instructions` and offering only the
  capabilities the backend has.
- **Self-healing** — starts whether or not the backend is up, reconnects with bounded jittered
  backoff, survives backend restarts, stops promptly on `SIGTERM`.
- **`backend_status`** tells a client whether the backend is reachable without provoking a
  failing call.
- Runs as a **`systemd --user` service**, or under a **per-user `supervisord`** where systemd
  user services are unavailable; configured by env vars or a standard `mcpServers` JSON file.
- **Fronts stdio backends too** — a backend started as a child process (`command`/`args`/`env`/
  `cwd` in the `mcpServers` config) gets the same serialization, schema parity and resilience
  as an HTTP backend. The proxy owns that subprocess's whole life: starts
  it on connect, keeps it across reconnects, and kills it on break or shutdown — no orphaned
  child left behind. An entry with FastMCP's transform keys (`tools`, `include_tags`,
  `exclude_tags`), which would put a nested proxy in front of the subprocess, is refused at
  startup.
- **Exposure control** — exact-name allow-list or deny-list per surface (tools, resources
  including templates, prompts). Hidden items are absent from listings and uncallable.
  Allow beats deny when both are set for a surface; `backend_status` is filtered like any
  other tool (with a startup warning if the operator's config excludes it). Off by default.

---

## Next

The next release. Each issue is merged and verified on its own; the milestone holds the release's
issues.

### v0.6.0 Description overrides

What agents are told about the backend becomes the operator's to tune; names and schema
structure stay exact.

- [#7] — override the descriptions of tools, arguments, prompts and resources, and the
  server's `instructions`, from an operator file. Depends on [#5].
- [#8] — honour the safe subset of FastMCP's `"tools"` key in the backend config, as text
  overrides and exposure-control entries; reject the rest at startup. Depends on [#1] and
  [#7].
- [#24] — a tool call made more than 300 s after the last tool listing puts a `tools/list` on
  the backend's queue, logged as that client's.

---

## Deferred

### Container image

The `systemd --user` and `supervisord` recipes both assume a Python toolchain and a checkout on
the host. That assumption doesn't hold everywhere the proxy is useful. Not planned for any of
the releases above.

**Intent.** A container image. Configure and inject the backend secret the way a container
operator normally would; no Python toolchain on the host; the secret never baked into an image
layer.

**Expected result.** `docker run` with your backend config and a port mapping, point an agent at
it, and the tools are there.

*Scope note:* the shipped image fronts backends **you already run** (HTTP). A stdio backend has
to live inside the container to be spawnable at all, so that case is served by a documented
"derive your own image from this one" pattern — add your backend's runtime on top of the base —
rather than by shipping an image that pretends to carry every runtime.

## Under consideration

Ideas without an issue or a release.

- **Agent guidance of the operator's own.** A skills directory and proxy-native prompts, served
  beside the backend's items, for guidance about using a backend in a particular setting that
  the backend's author could not write.
- **Let some tools skip the queue.** Blanket serialization is the right default and the reason
  this exists — but it's a tax. A cheap, read-only, concurrency-safe tool that gets called
  constantly ends up queued behind an expensive write. You know which of your backend's tools
  are safe; the proxy can't, and shouldn't guess. Tools marked concurrency-safe by name would
  run in a second lane with its own limit, alongside the serialized one — not bypassing the
  queue, so total load on the backend stays bounded (serialized limit + concurrent limit). Off
  by default, explicit, never a mode you can fall into. Exempt calls may overtake queued ones,
  with deliberately no ordering guarantee between the two lanes, and a hung exempt call still
  can't delay the serialized queue.
- **Metrics.** Queue depth, wait time and backend flaps are logged but nothing aggregates them.
  Worth doing if the logs stop being enough.
- **Glob patterns in exposure rules.** Exact names first; patterns only if the lists get
  unwieldy in practice.
- **Prompts and resources exposed as tools.** Some clients use only tools and never read prompts
  or resources. Presenting the backend's prompts and resources as tools would reach those
  clients; since parity allows no item of the proxy's own besides `backend_status`, it would
  be opt-in.

## Not planned

- **Multiple backends / aggregation.** One backend per proxy process is a deliberate invariant,
  not a limitation waiting to be lifted. Run a second proxy.
- **Changing backend behaviour.** The proxy controls *access* to a backend; it never retries,
  reshapes or papers over its semantics. A tool that errors keeps erroring.
- **Authenticating the proxy's own clients.** It's a local, loopback-scoped service. (Container
  deployment may make this worth revisiting.)
- **Publishing to a package index.** Deployment recipes, yes; distribution, no.

[#1]: https://github.com/eoc-llm-tooling/mcp-call-orchestrator-proxy/issues/1
[#5]: https://github.com/eoc-llm-tooling/mcp-call-orchestrator-proxy/issues/5
[#7]: https://github.com/eoc-llm-tooling/mcp-call-orchestrator-proxy/issues/7
[#8]: https://github.com/eoc-llm-tooling/mcp-call-orchestrator-proxy/issues/8
[#24]: https://github.com/eoc-llm-tooling/mcp-call-orchestrator-proxy/issues/24
