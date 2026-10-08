# Roadmap

Where the proxy is today, and what it is meant to become. Ordered by intent, not by date —
nothing here is a scheduled commitment, and the order changes when a real need does.

## Where it is today

- **Exact parity** with the backend's tools, resources and prompts — real names, original
  schemas, nothing hand-synced.
- **Calls are serialized** through one FIFO queue with a configurable limit (default `1`) and
  a per-call timeout, so a single hung call can't wedge the rest.
- **Multi-session safe** — independent clients connect over HTTP at once without cross-talk;
  sessions terminate at the proxy.
- **Self-healing** — starts whether or not the backend is up, reconnects with bounded jittered
  backoff, survives backend restarts, stops promptly on `SIGTERM`.
- **`backend_status`** tells a client whether the backend is reachable without provoking a
  failing call.
- Runs as a **`systemd --user` service**; configured by env vars or a standard `mcpServers`
  JSON file.
- **Fronts stdio backends too** — a backend started as a child process (`command`/`args`/`env`/
  `cwd` in the `mcpServers` config) gets the same serialization, session termination, schema
  parity and resilience as an HTTP backend. The proxy owns that subprocess's whole life: starts
  it on connect, keeps it across reconnects, and kills it on break or shutdown — no orphaned
  child left behind.
- **Exposure control** — exact-name allow-list or deny-list per surface (tools, resources
  including templates, prompts). Hidden items are absent from listings and uncallable.
  Allow beats deny when both are set for a surface; `backend_status` is filtered like any
  other tool (with a startup warning if the operator's config excludes it). Off by default.

---

## Next: run it where you run things

One `systemd --user` recipe assumes systemd, a user session, and lingering enabled. That
assumption doesn't hold everywhere the proxy is useful.

**Intent.** A container image, and a process-supervisor configuration for hosts without systemd
user services. Configure and inject the backend secret the way a container operator normally
would; no Python toolchain on the host; the secret never baked into an image layer.

**Expected result.** `docker run` with your backend config and a port mapping, point an agent at
it, and the tools are there. Under a supervisor: drop in the config, kill the process, it comes
back, logs land where the supervisor puts them, and stop is clean and prompt.

*Scope note:* the shipped image fronts backends **you already run** (HTTP). A stdio backend has
to live inside the container to be spawnable at all, so that case is served by a documented
"derive your own image from this one" pattern — add your backend's runtime on top of the base —
rather than by shipping an image that pretends to carry every runtime.

## Later: let some tools skip the queue

Blanket serialization is the right default and the reason this exists — but it's a tax. A cheap,
read-only, concurrency-safe tool that gets called constantly ends up queued behind an expensive
write. You know which of your backend's tools are safe; the proxy can't, and shouldn't guess.

**Intent.** Mark specific tools, by name, as concurrency-safe. They run in a second lane with
its own limit, alongside the serialized one — they do **not** bypass the queue entirely, so
total load on the backend stays bounded (serialized limit + concurrent limit). Off by default,
explicit, never a mode you can fall into.

**Expected result.** Under load, the backend serves several exempt calls at once while every
other tool still runs strictly one at a time. Exempt calls may overtake queued ones — that's the
point, and there is deliberately no ordering guarantee between the two lanes. A hung exempt call
still can't delay the serialized queue.

---

## Under consideration

- **Metrics.** Queue depth, wait time and backend flaps are logged but nothing aggregates them.
  Worth doing if the logs stop being enough.
- **Glob patterns in exposure rules.** Exact names first; patterns only if the lists get
  unwieldy in practice.
- **Quiet down "unsupported method" noise from `list_prompts`/`list_resources`.** A backend that
  simply doesn't implement prompts or resources (e.g. a tools-only server) answers with a normal
  JSON-RPC `Method not found`, but that isn't one of `resilient.py`'s `_CONNECTION_ERRORS`, so it
  falls through to the orchestrator's generic `except Exception` path and logs an ERROR with a
  full traceback — on every client connection that does capability discovery. Not a fault, just
  mis-leveled. Worth a real fix (recognize "unsupported method" distinctly, log it quietly) once
  we're touching that error-handling path again.

## Not planned

- **Multiple backends / aggregation.** One backend per proxy process is a deliberate invariant,
  not a limitation waiting to be lifted. Run a second proxy.
- **Changing backend behaviour.** The proxy controls *access* to a backend; it never retries,
  reshapes or papers over its semantics. A tool that errors keeps erroring.
- **Authenticating the proxy's own clients.** It's a local, loopback-scoped service. (Container
  deployment may make this worth revisiting.)
- **Publishing to a package index.** Deployment recipes, yes; distribution, no.
