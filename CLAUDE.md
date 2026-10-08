# CLAUDE.md

Orientation for agents working in this repository. Keep it short; the deeper material lives in
`ARCHITECTURE.md` and `docs/`.

## Before committing

- Run `uv run poe gate` and commit only when it passes.
- Never commit paths from your own machine, IP or MAC addresses, internal host names, tokens or
  keys; use the documented defaults or placeholders.
- Work on a branch and open a pull request; never push to `main`.

## Purpose

`mcp-call-orchestrator-proxy` is a **resilient, session-terminating, call-serializing MCP
proxy** in front of a *single* backend MCP server. It makes a backend that can't handle
concurrent clients safe for several at once: each client's session terminates at the proxy,
calls to the backend are queued (default: fully serialized), and the connection self-heals
when the backend comes and goes. The backend's tools are mirrored with **exact schema
parity**, plus one added `backend_status` tool. Treat it as backend-agnostic: nothing in the
code or the shipped artifacts may presume a particular backend product.

## Start here

- **[`ARCHITECTURE.md`](ARCHITECTURE.md)** — canonical technical overview: components, call
  flow, invariants, resilience model. **Read this instead of re-deriving the design from
  source.**
- **[`docs/coding-standards.md`](docs/coding-standards.md)** — conventions to match.
- **[`docs/testing.md`](docs/testing.md)** — the test tiers, the shared harness, and which
  backend to test a given claim against. **Read before adding a test.**
- **[`README.md`](README.md)** — user-facing install, configuration and usage.
- **[`docs/roadmap.md`](docs/roadmap.md)** — what's done, what's open.
- **[`docs/issues.md`](docs/issues.md)** — defects found, each with its root cause and fix.

## Architecture at a glance

One process, one backend connection, one queue. Every backend-facing call flows:

```
client → FastMCPProxy (server.py) → QueuedMCPBackend (backend.py)
       → CallOrchestrator (orchestrator.py) → ResilientBackend (resilient.py)
       → real fastmcp.Client (client.py) → backend MCP server
```

`app.py::create_proxy_app` is the composition root that wires all of it and owns the
lifespan. Every layer speaks the `MCPBackendClient` Protocol (`interfaces.py`), which is why
they stack without adapters and are individually testable.

## Commands

**Use `uv` for everything — never bare `python` or `pip`.**

```bash
uv sync                                   # create .venv + install
uv run poe gate                           # lint + format-check + mypy + unit + component + integration*
uv run poe format                         # auto-format
uv run poe test-int                       # integration tests only, as a standalone shortcut
uv run poe check-py313                    # optional: full gate on Python 3.13, own venv (forward-compat only; floor stays 3.12)
uv run mcp-call-orchestrator-proxy run    # start the proxy
```

\* The integration tier only runs for real when `tests/integration/backend.json` is configured;
without it, that step is skipped cleanly (still exit 0) and says why.

## Conventions (summary — details in `docs/coding-standards.md`)

- **Python 3.12+**, and the floor is enforced, not just declared: `pyproject`, the README and
  the ruff/mypy targets all agree on 3.12. Don't add 3.13-only syntax — `uv run poe check-py313`
  is where forward-compatibility with 3.13 gets verified, separately, not by loosening the floor.
- Async-first; `from __future__ import annotations`; fully typed under **strict mypy**; ruff
  (88 cols, double quotes).
- Protocol-based dependency inversion, one concern per module, all wiring in the composition
  root — no global state, no import-time I/O.
- Secrets only from env / `.env` / the backend JSON. Never hard-coded.

## Tests

Three tiers — `tests/unit` and `tests/component` are offline; `tests/integration` runs against
a real backend when the gitignored `tests/integration/backend.json` exists (copy
`backend.example.json`) and skips cleanly when it doesn't. All three run from `uv run poe gate`.

**[`docs/testing.md`](docs/testing.md) is the canonical account** — what each tier is for, the
shared harness in `tests/conftest.py`, and the judgement call that matters: whether a claim
needs an in-process mock or the **real MCP server the tests spawn as a child process**. Read
it before adding a test rather than copying the nearest existing one.

## Gotchas

- **Exactly one backend.** A config with 2+ servers is rejected on purpose — a composite mount
  would prefix tool names (`{server}_{tool}`) and break schema parity.
- **`FastMCPProxy` is constructed directly**, not via `create_proxy()`. `create_proxy()` would
  treat our queue-aware facade as a raw transport and wrap it in a fresh client. See the
  comment in `server.py`.
- **`initialize()` never raises when the backend is down** ("stay connectable, degrade") — clients
  must always be able to handshake with the proxy. Don't "fix" this into raising.
- **Break detection is network-silent** — no timed pings against the backend. Don't add a health-check ping.
- **Stdio backends force `keep_alive=False`** unconditionally in `client.py::_single_server_transport`,
  regardless of what the config JSON says. Don't "simplify" that away — it's what stops a stdio
  backend's subprocess from being orphaned across reconnects and on shutdown.

## Keeping docs current

`ARCHITECTURE.md` is what a fresh session reads instead of the source — it must stay true.
Update it when a layer is added/removed/reordered, the `MCPBackendClient` contract changes, a
design invariant changes, or the resilience/config model changes (cross-check the variable
table in `README.md`).

`docs/testing.md` plays the same role for the test suite. Update it when a tier is added or
changes purpose, the shared harness in `tests/conftest.py` gains or loses a fixture, the probe
backend server gains a tool, or the rule for choosing a backend to test against changes.

Don't log routine fixes in either — that's git history.

Record a defect found in operation or review in `docs/issues.md` — append-only entries; a
confirmed root cause is corrected by adding a finding, not by rewriting it. Put forward feature
work in `docs/roadmap.md`. Write docs in reference register (what the system is and why);
instructions to agents belong in this file.
